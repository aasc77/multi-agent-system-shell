#!/usr/bin/env python3
"""
Unit tests for the Dog Tracker Service.

Tests cover: config loading, state machine transitions, PID + EMA tracking
logic, dog selection, PTZ rate limiting, and command parsing.

Usage:
    cd /Users/angelserrano/Repositories/multi-agent-system-shell
    python3 -m pytest tests/test_dog_tracker.py -v
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# Add service to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "dog-tracker"))

from service import (
    DogTrackerService,
    PTZController,
    TrackingPipeline,
    TrackingState,
    load_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    """Load the default config."""
    return load_config(os.path.join(
        os.path.dirname(__file__), "..", "services", "dog-tracker", "config.yaml"
    ))


@pytest.fixture
def mock_ptz():
    """PTZ controller with mocked ONVIF internals."""
    ptz = PTZController("192.168.1.100", 80, "admin", "pass", max_rate_hz=10)
    ptz._connected = True
    ptz._ptz = MagicMock()
    ptz._profile_token = "test_profile"
    ptz._ptz.create_type.return_value = MagicMock()
    return ptz


@pytest.fixture
def pipeline(cfg, mock_ptz):
    """TrackingPipeline with mocked PTZ (no model/stream)."""
    return TrackingPipeline(cfg, mock_ptz)


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_load_default_config(self, cfg):
        assert cfg["camera"]["onvif_port"] == 8000
        assert cfg["pid"]["kp"] == 0.3
        assert cfg["tracking"]["dead_zone"] == 0.05
        assert cfg["detection"]["dog_class_id"] == 16

    def test_env_override(self, tmp_path, cfg):
        import yaml
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(cfg))

        with patch.dict(os.environ, {"RTSP_URL": "rtsp://test:554/stream"}):
            loaded = load_config(str(config_file))
            assert loaded["camera"]["rtsp_url"] == "rtsp://test:554/stream"

    def test_nats_config(self, cfg):
        assert cfg["nats"]["inbox"] == "agents.dogtracker.inbox"
        assert cfg["nats"]["outbox"] == "agents.dogtracker.outbox"
        assert cfg["nats"]["consumer"] == "dogtracker-service"


# ---------------------------------------------------------------------------
# State Machine Tests
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_initial_state(self, pipeline):
        assert pipeline.state == TrackingState.IDLE

    def test_idle_to_tracking(self, pipeline):
        announcement = pipeline._set_state(TrackingState.TRACKING)
        assert pipeline.state == TrackingState.TRACKING
        assert "Tracking started" in announcement

    def test_tracking_to_searching(self, pipeline):
        pipeline._set_state(TrackingState.TRACKING)
        announcement = pipeline._set_state(TrackingState.SEARCHING)
        assert pipeline.state == TrackingState.SEARCHING
        assert "Searching" in announcement

    def test_searching_to_returning(self, pipeline):
        pipeline._set_state(TrackingState.SEARCHING)
        announcement = pipeline._set_state(TrackingState.RETURNING_HOME)
        assert pipeline.state == TrackingState.RETURNING_HOME
        assert "home" in announcement.lower()

    def test_returning_to_idle(self, pipeline):
        pipeline._set_state(TrackingState.RETURNING_HOME)
        announcement = pipeline._set_state(TrackingState.IDLE)
        assert pipeline.state == TrackingState.IDLE
        assert "idle" in announcement.lower()

    def test_same_state_no_announcement(self, pipeline):
        pipeline._set_state(TrackingState.TRACKING)
        announcement = pipeline._set_state(TrackingState.TRACKING)
        assert announcement is None


# ---------------------------------------------------------------------------
# Dog Selection Tests
# ---------------------------------------------------------------------------

class TestDogSelection:
    def _make_results(self, dogs):
        """Create mock YOLO results with dog detections."""
        results = MagicMock()
        boxes = []
        for d in dogs:
            box = MagicMock()
            box.cls = [MagicMock()]
            box.cls[0].__int__ = lambda self, _d=d: _d["cls"]
            box.cls[0].__float__ = lambda self, _d=d: float(_d["cls"])
            # xyxy[0] must have .tolist() like a torch tensor
            xyxy_tensor = MagicMock()
            xyxy_tensor.tolist.return_value = d["bbox"]
            box.xyxy = [xyxy_tensor]
            box.id = [MagicMock()] if d.get("track_id") is not None else None
            if box.id:
                box.id[0].__int__ = lambda self, _d=d: _d["track_id"]
                box.id[0].__float__ = lambda self, _d=d: float(_d["track_id"])
            boxes.append(box)

        results.boxes = boxes
        return results

    def test_selects_dog_class_only(self, pipeline):
        results = self._make_results([
            {"cls": 0, "bbox": [0, 0, 100, 100], "track_id": 1},   # person
            {"cls": 16, "bbox": [50, 50, 200, 200], "track_id": 2},  # dog
        ])
        dog = pipeline._select_dog(results)
        assert dog is not None
        assert dog["track_id"] == 2

    def test_selects_largest_dog(self, pipeline):
        results = self._make_results([
            {"cls": 16, "bbox": [0, 0, 50, 50], "track_id": 1},     # small
            {"cls": 16, "bbox": [0, 0, 200, 200], "track_id": 2},   # big
        ])
        dog = pipeline._select_dog(results)
        assert dog["track_id"] == 2

    def test_sticks_to_tracked_id(self, pipeline):
        pipeline._track_id = 1
        results = self._make_results([
            {"cls": 16, "bbox": [0, 0, 50, 50], "track_id": 1},     # small but tracked
            {"cls": 16, "bbox": [0, 0, 200, 200], "track_id": 2},   # bigger
        ])
        dog = pipeline._select_dog(results)
        assert dog["track_id"] == 1  # sticks to current

    def test_no_dogs(self, pipeline):
        results = self._make_results([
            {"cls": 0, "bbox": [0, 0, 100, 100], "track_id": 1},  # person only
        ])
        dog = pipeline._select_dog(results)
        assert dog is None

    def test_empty_results(self, pipeline):
        results = MagicMock()
        results.boxes = []
        dog = pipeline._select_dog(results)
        assert dog is None


# ---------------------------------------------------------------------------
# PID + Detection Processing Tests
# ---------------------------------------------------------------------------

class TestProcessDetection:
    def test_centered_dog_stops_ptz(self, pipeline, mock_ptz):
        """Dog at frame center should trigger PTZ stop."""
        pipeline._set_state(TrackingState.TRACKING)
        dog = {"cx": 960, "cy": 540, "track_id": 1}  # center of 1920x1080
        pipeline._process_detection(dog)
        mock_ptz._ptz.Stop.assert_called()

    def test_offset_dog_moves_ptz(self, pipeline, mock_ptz):
        """Dog far from center should trigger ContinuousMove."""
        pipeline._set_state(TrackingState.IDLE)
        dog = {"cx": 1600, "cy": 200, "track_id": 1}  # top-right
        pipeline._process_detection(dog)
        # Should have called ContinuousMove
        mock_ptz._ptz.create_type.assert_called_with("ContinuousMove")

    def test_ema_smoothing(self, pipeline):
        """EMA should smooth positions over multiple frames."""
        pipeline._smooth_x = 0.0
        pipeline._smooth_y = 0.0

        # Simulate sudden jump to right
        dog = {"cx": 1920, "cy": 540, "track_id": 1}  # far right
        pipeline._process_detection(dog)

        # EMA should not jump to 1.0 immediately
        assert pipeline._smooth_x < 0.5  # dampened

    def test_transitions_to_tracking(self, pipeline):
        """First detection should transition from IDLE to TRACKING."""
        assert pipeline.state == TrackingState.IDLE
        dog = {"cx": 1000, "cy": 600, "track_id": 1}
        announcement = pipeline._process_detection(dog)
        assert pipeline.state == TrackingState.TRACKING
        assert announcement is not None
        assert "Tracking" in announcement


# ---------------------------------------------------------------------------
# Lost Target Tests
# ---------------------------------------------------------------------------

class TestProcessLost:
    def test_idle_stays_idle(self, pipeline):
        """Lost target in IDLE state should do nothing."""
        result = pipeline._process_lost()
        assert result is None
        assert pipeline.state == TrackingState.IDLE

    def test_tracking_to_searching(self, pipeline):
        """After continue_seconds, should transition to SEARCHING."""
        pipeline._set_state(TrackingState.TRACKING)
        pipeline._last_velocity = (0.3, 0.0)

        # First lost call starts the timer
        pipeline._process_lost()
        assert pipeline._lost_time > 0

        # Simulate time passing beyond continue timeout
        pipeline._lost_time = time.monotonic() - pipeline._continue_timeout - 0.1
        announcement = pipeline._process_lost()
        assert pipeline.state == TrackingState.SEARCHING

    def test_searching_to_returning(self, pipeline):
        """After search_seconds, should transition to RETURNING_HOME."""
        pipeline._set_state(TrackingState.TRACKING)
        pipeline._last_velocity = (0.3, 0.0)
        pipeline._lost_time = time.monotonic() - pipeline._search_timeout - 0.1
        pipeline._process_lost()
        assert pipeline.state == TrackingState.RETURNING_HOME

    def test_returning_to_idle(self, pipeline):
        """After home_seconds, should transition to IDLE."""
        pipeline._set_state(TrackingState.TRACKING)
        pipeline._last_velocity = (0.3, 0.0)
        pipeline._lost_time = time.monotonic() - pipeline._home_timeout - 0.1
        pipeline._process_lost()
        assert pipeline.state == TrackingState.IDLE

    def test_pid_reset_on_idle(self, pipeline):
        """PIDs should be reset when returning to IDLE."""
        pipeline._set_state(TrackingState.TRACKING)
        pipeline._last_velocity = (0.3, 0.1)
        pipeline._smooth_x = 0.5
        pipeline._smooth_y = 0.3
        pipeline._lost_time = time.monotonic() - pipeline._home_timeout - 0.1
        pipeline._process_lost()

        assert pipeline._smooth_x == 0.0
        assert pipeline._smooth_y == 0.0
        assert pipeline._track_id is None


# ---------------------------------------------------------------------------
# PTZ Controller Tests
# ---------------------------------------------------------------------------

class TestPTZController:
    def test_rate_limiting(self, mock_ptz):
        """Should not send commands faster than max_rate_hz."""
        mock_ptz._min_interval = 0.1  # 10 Hz
        mock_ptz._last_cmd_time = time.monotonic()  # just sent

        mock_ptz.continuous_move(0.5, 0.5)
        # Should not have called ContinuousMove (too soon)
        mock_ptz._ptz.ContinuousMove.assert_not_called()

    def test_allows_after_interval(self, mock_ptz):
        """Should allow command after rate limit interval."""
        mock_ptz._last_cmd_time = time.monotonic() - 0.2  # 200ms ago
        mock_ptz._min_interval = 0.1  # 10 Hz

        mock_ptz.continuous_move(0.5, 0.3)
        mock_ptz._ptz.ContinuousMove.assert_called_once()

    def test_stop(self, mock_ptz):
        mock_ptz.stop()
        mock_ptz._ptz.Stop.assert_called_once()

    def test_connected_property(self, mock_ptz):
        assert mock_ptz.connected is True


# ---------------------------------------------------------------------------
# Command Parsing Tests
# ---------------------------------------------------------------------------

class TestCommandParsing:
    @pytest.fixture
    def service(self, cfg):
        return DogTrackerService(cfg)

    @pytest.mark.asyncio
    async def test_status_offline(self, service):
        result = await service._execute("status")
        assert "OFFLINE" in result

    @pytest.mark.asyncio
    async def test_unknown_command(self, service):
        result = await service._execute("dance")
        assert "Unknown command" in result
        assert "Available" in result

    @pytest.mark.asyncio
    async def test_start_no_rtsp(self, service):
        result = await service._execute("start")
        assert "RTSP URL not configured" in result

    @pytest.mark.asyncio
    async def test_stop_not_running(self, service):
        result = await service._execute("stop")
        assert "not running" in result

    @pytest.mark.asyncio
    async def test_home_no_ptz(self, service):
        result = await service._execute("home")
        assert "not connected" in result

    @pytest.mark.asyncio
    async def test_start_already_running(self, service):
        """Should return 'Already running' when pipeline is active."""
        mock_pipeline = MagicMock()
        mock_pipeline._running = True
        mock_pipeline.state = TrackingState.IDLE
        service._pipeline = mock_pipeline
        result = await service._execute("start")
        assert "Already running" in result

    @pytest.mark.asyncio
    async def test_stop_then_start_allowed(self, service):
        """After stop, start should not report 'Already running'."""
        mock_pipeline = MagicMock()
        mock_pipeline._running = True
        mock_pipeline.state = TrackingState.IDLE
        service._pipeline = mock_pipeline
        service._pipeline_task = MagicMock()
        # Stop
        result = await service._execute("stop")
        assert "stopped" in result.lower()
        # Start should not say already running (but will fail on RTSP)
        result = await service._execute("start")
        assert "Already running" not in result


# ---------------------------------------------------------------------------
# Stats Tests
# ---------------------------------------------------------------------------

class TestStats:
    def test_initial_stats(self, pipeline):
        stats = pipeline.stats
        assert stats["state"] == "IDLE"
        assert stats["frames_processed"] == 0
        assert stats["detections_count"] == 0
        assert stats["current_track_id"] is None
