#!/usr/bin/env python3
"""
Dog Tracker Service — YOLO + ByteTrack + ONVIF PTZ real-time dog tracking.

Detects dogs via YOLOv11 on an RTSP camera feed and keeps them centered
in frame by controlling PTZ via ONVIF with a PID controller.

NATS commands:
    send_to_agent(target_agent="dogtracker", message="start")
    send_to_agent(target_agent="dogtracker", message="stop")
    send_to_agent(target_agent="dogtracker", message="status")
    send_to_agent(target_agent="dogtracker", message="home")

State machine: IDLE -> TRACKING -> SEARCHING -> RETURNING_HOME -> IDLE

Usage:
    python3 services/dog-tracker/service.py
    python3 services/dog-tracker/service.py --config path/to/config.yaml
    python3 services/dog-tracker/service.py --once status
"""

import asyncio
import enum
import json
import logging
import os
import signal
import sys
import time

import cv2
import nats
import yaml
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from simple_pid import PID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [dog-tracker] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "config.yaml")


def load_config(path: str | None = None) -> dict:
    """Load YAML config with env var overrides."""
    config_path = path or DEFAULT_CONFIG
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    # Allow env overrides for deployment
    if os.environ.get("RTSP_URL"):
        cfg["camera"]["rtsp_url"] = os.environ["RTSP_URL"]
    if os.environ.get("ONVIF_HOST"):
        cfg["camera"]["onvif_host"] = os.environ["ONVIF_HOST"]
    if os.environ.get("ONVIF_PASSWORD"):
        cfg["camera"]["onvif_password"] = os.environ["ONVIF_PASSWORD"]
    if os.environ.get("NATS_URL"):
        cfg["nats"]["url"] = os.environ["NATS_URL"]
    if os.environ.get("LOG_LEVEL"):
        cfg["log_level"] = os.environ["LOG_LEVEL"]

    return cfg


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------

class TrackingState(enum.Enum):
    IDLE = "IDLE"
    TRACKING = "TRACKING"
    SEARCHING = "SEARCHING"
    RETURNING_HOME = "RETURNING_HOME"


# ---------------------------------------------------------------------------
# PTZ Controller
# ---------------------------------------------------------------------------

class PTZController:
    """Wraps ONVIF PTZ commands with rate limiting."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 max_rate_hz: float = 10.0):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._max_rate_hz = max_rate_hz
        self._min_interval = 1.0 / max_rate_hz
        self._last_cmd_time = 0.0
        self._cam = None
        self._ptz = None
        self._profile_token = None
        self._connected = False

    def connect(self):
        """Initialize ONVIF connection."""
        from onvif import ONVIFCamera

        logger.info("Connecting to ONVIF camera at %s:%d", self._host, self._port)
        self._cam = ONVIFCamera(self._host, self._port, self._user, self._password)
        media = self._cam.create_media_service()
        self._ptz = self._cam.create_ptz_service()
        profiles = media.GetProfiles()
        self._profile_token = profiles[0].token
        self._connected = True
        logger.info("ONVIF connected, profile: %s", self._profile_token)

    @property
    def connected(self) -> bool:
        return self._connected

    def continuous_move(self, pan_vel: float, tilt_vel: float):
        """Send ContinuousMove command, rate-limited."""
        now = time.monotonic()
        if now - self._last_cmd_time < self._min_interval:
            return

        request = self._ptz.create_type("ContinuousMove")
        request.ProfileToken = self._profile_token
        request.Velocity = {
            "PanTilt": {"x": pan_vel, "y": tilt_vel},
            "Zoom": {"x": 0.0},
        }
        self._ptz.ContinuousMove(request)
        self._last_cmd_time = now

    def stop(self):
        """Stop all PTZ movement."""
        self._ptz.Stop({"ProfileToken": self._profile_token})

    def goto_home(self):
        """Move to home preset (preset 1)."""
        try:
            request = self._ptz.create_type("GotoHomePosition")
            request.ProfileToken = self._profile_token
            self._ptz.GotoHomePosition(request)
        except Exception:
            # Fallback: absolute move to 0,0
            request = self._ptz.create_type("AbsoluteMove")
            request.ProfileToken = self._profile_token
            request.Position = {
                "PanTilt": {"x": 0.0, "y": 0.0},
                "Zoom": {"x": 0.5},
            }
            request.Speed = {"PanTilt": {"x": 1.0, "y": 1.0}}
            self._ptz.AbsoluteMove(request)

    def get_position(self) -> dict | None:
        """Get current PTZ position."""
        try:
            status = self._ptz.GetStatus({"ProfileToken": self._profile_token})
            pos = status.Position
            return {
                "pan": pos.PanTilt.x,
                "tilt": pos.PanTilt.y,
                "zoom": pos.Zoom.x if pos.Zoom else 0,
            }
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Tracking Pipeline
# ---------------------------------------------------------------------------

class TrackingPipeline:
    """RTSP capture + YOLO detection + ByteTrack + PID control loop."""

    def __init__(self, cfg: dict, ptz: PTZController):
        self._cfg = cfg
        self._ptz = ptz
        self._state = TrackingState.IDLE
        self._running = False
        self._model = None
        self._cap = None

        # PID controllers for pan and tilt
        pid_cfg = cfg["pid"]
        self._pid_pan = PID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            setpoint=0, output_limits=(pid_cfg["output_min"], pid_cfg["output_max"]),
        )
        self._pid_tilt = PID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            setpoint=0, output_limits=(pid_cfg["output_min"], pid_cfg["output_max"]),
        )

        # Tracking state
        tracking_cfg = cfg["tracking"]
        self._dead_zone = tracking_cfg["dead_zone"]
        self._ema_alpha = tracking_cfg["ema_alpha"]
        self._invert_tilt = tracking_cfg.get("invert_tilt", True)
        self._smooth_x = 0.0
        self._smooth_y = 0.0
        self._last_velocity = (0.0, 0.0)
        self._lost_time = 0.0
        self._track_id = None

        # Lost target timers
        lost_cfg = cfg["lost_target"]
        self._continue_timeout = lost_cfg["continue_seconds"]
        self._search_timeout = lost_cfg["search_seconds"]
        self._home_timeout = lost_cfg["home_seconds"]

        # Frame dimensions (set on first frame)
        self._frame_w = cfg["camera"]["resolution_width"]
        self._frame_h = cfg["camera"]["resolution_height"]

        # Stats
        self._frames_processed = 0
        self._detections_count = 0

    def _load_model(self):
        """Load YOLO model."""
        from ultralytics import YOLO
        det_cfg = self._cfg["detection"]
        logger.info("Loading YOLO model: %s", det_cfg["model"])
        self._model = YOLO(det_cfg["model"])
        # Warm up
        import numpy as np
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        self._model(dummy, verbose=False)
        logger.info("YOLO model loaded and warmed up")

    def _open_stream(self):
        """Open RTSP stream."""
        rtsp_url = self._cfg["camera"]["rtsp_url"]
        logger.info("Opening RTSP stream: %s", rtsp_url)
        self._cap = cv2.VideoCapture(rtsp_url)
        if not self._cap.isOpened():
            raise RuntimeError(f"Failed to open RTSP stream: {rtsp_url}")
        # Read actual dimensions
        self._frame_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._frame_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info("Stream opened: %dx%d", self._frame_w, self._frame_h)

    @property
    def state(self) -> TrackingState:
        return self._state

    @property
    def stats(self) -> dict:
        return {
            "state": self._state.value,
            "frames_processed": self._frames_processed,
            "detections_count": self._detections_count,
            "current_track_id": self._track_id,
        }

    def _set_state(self, new_state: TrackingState) -> str | None:
        """Transition state, return announcement text if state changed."""
        if new_state == self._state:
            return None
        old = self._state
        self._state = new_state
        logger.info("State: %s -> %s", old.value, new_state.value)

        announcements = {
            TrackingState.TRACKING: "Dog detected in backyard. Tracking started.",
            TrackingState.SEARCHING: "Dog lost from view. Searching.",
            TrackingState.RETURNING_HOME: "Dog not found. Returning camera to home position.",
            TrackingState.IDLE: "Camera returned to home. Tracking idle.",
        }
        return announcements.get(new_state)

    def _select_dog(self, results) -> dict | None:
        """Select the best dog detection from YOLO results.

        Sticks to current ByteTrack ID if still present. Otherwise picks
        the largest bounding box (closest dog).
        """
        if results.boxes is None or len(results.boxes) == 0:
            return None

        dog_class = self._cfg["detection"]["dog_class_id"]
        dogs = []

        for box in results.boxes:
            cls_id = int(box.cls[0])
            if cls_id != dog_class:
                continue
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            track_id = int(box.id[0]) if box.id is not None else None
            area = (x2 - x1) * (y2 - y1)
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            dogs.append({
                "cx": cx, "cy": cy,
                "area": area, "track_id": track_id,
                "bbox": [x1, y1, x2, y2],
            })

        if not dogs:
            return None

        # Prefer current track ID
        if self._track_id is not None:
            for d in dogs:
                if d["track_id"] == self._track_id:
                    return d

        # Largest bbox (closest dog)
        best = max(dogs, key=lambda d: d["area"])
        self._track_id = best["track_id"]
        return best

    async def run(self, event_callback=None):
        """Main tracking loop. event_callback(text) for announcements."""
        self._load_model()
        self._open_stream()
        self._running = True
        self._state = TrackingState.IDLE

        det_cfg = self._cfg["detection"]
        tracking_cfg = self._cfg["tracking"]
        idle_interval = 1.0 / tracking_cfg["idle_fps"]
        tracking_interval = 1.0 / tracking_cfg["tracking_fps"]

        logger.info("Tracking pipeline started")

        try:
            while self._running:
                loop_start = time.monotonic()

                # Adaptive frame rate
                interval = tracking_interval if self._state == TrackingState.TRACKING else idle_interval

                ret, frame = self._cap.read()
                if not ret:
                    logger.warning("Frame read failed, reopening stream")
                    self._cap.release()
                    await asyncio.sleep(2)
                    self._open_stream()
                    continue

                self._frames_processed += 1

                # Run YOLO + ByteTrack
                results = self._model.track(
                    frame,
                    tracker=det_cfg["tracker"],
                    classes=[det_cfg["dog_class_id"]],
                    conf=det_cfg["confidence"],
                    persist=True,
                    verbose=False,
                )[0]

                dog = self._select_dog(results)

                if dog:
                    self._detections_count += 1
                    announcement = self._process_detection(dog)
                else:
                    announcement = self._process_lost()

                if announcement and event_callback:
                    await event_callback(announcement)

                # Sleep only the remaining time to hit target frame rate
                elapsed = time.monotonic() - loop_start
                sleep_time = max(0, interval - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        finally:
            if self._cap:
                self._cap.release()
            logger.info("Tracking pipeline stopped")

    def _process_detection(self, dog: dict) -> str | None:
        """Handle a dog detection — update PID, send PTZ commands."""
        # Normalize offset to [-1, 1]
        offset_x = (dog["cx"] - self._frame_w / 2) / (self._frame_w / 2)
        offset_y = (dog["cy"] - self._frame_h / 2) / (self._frame_h / 2)

        # EMA smoothing
        self._smooth_x = self._ema_alpha * offset_x + (1 - self._ema_alpha) * self._smooth_x
        self._smooth_y = self._ema_alpha * offset_y + (1 - self._ema_alpha) * self._smooth_y

        # Transition to TRACKING
        announcement = self._set_state(TrackingState.TRACKING)
        self._lost_time = 0.0

        # Dead zone check
        if abs(self._smooth_x) < self._dead_zone and abs(self._smooth_y) < self._dead_zone:
            if self._ptz.connected:
                self._ptz.stop()
            self._last_velocity = (0.0, 0.0)
            return announcement

        # PID output
        pan_vel = self._pid_pan(self._smooth_x)
        tilt_vel = self._pid_tilt(self._smooth_y)
        self._last_velocity = (pan_vel, tilt_vel)

        if self._ptz.connected:
            tilt_out = -tilt_vel if self._invert_tilt else tilt_vel
            self._ptz.continuous_move(pan_vel, tilt_out)

        return announcement

    def _process_lost(self) -> str | None:
        """Handle no detection — manage lost target states."""
        now = time.monotonic()
        announcement = None

        if self._state == TrackingState.IDLE:
            return None

        if self._lost_time == 0.0:
            self._lost_time = now

        elapsed = now - self._lost_time

        if elapsed < self._continue_timeout:
            # Keep moving in last direction
            if self._ptz.connected and self._last_velocity != (0.0, 0.0):
                pan, tilt = self._last_velocity
                self._ptz.continuous_move(pan * 0.5, tilt * 0.5)

        elif elapsed < self._search_timeout:
            # Slow pan in last direction
            announcement = self._set_state(TrackingState.SEARCHING)
            if self._ptz.connected:
                pan, _ = self._last_velocity
                direction = 0.2 if pan >= 0 else -0.2
                self._ptz.continuous_move(direction, 0.0)

        elif elapsed < self._home_timeout:
            # Return to home
            announcement = self._set_state(TrackingState.RETURNING_HOME)
            if self._ptz.connected:
                self._ptz.stop()
                self._ptz.goto_home()

        else:
            # Back to idle
            announcement = self._set_state(TrackingState.IDLE)
            self._lost_time = 0.0
            self._track_id = None
            self._pid_pan.reset()
            self._pid_tilt.reset()
            self._smooth_x = 0.0
            self._smooth_y = 0.0
            if self._ptz.connected:
                self._ptz.stop()

        return announcement

    def stop(self):
        """Stop the tracking loop."""
        self._running = False
        if self._ptz.connected:
            self._ptz.stop()


# ---------------------------------------------------------------------------
# NATS Service
# ---------------------------------------------------------------------------

class DogTrackerService:
    """NATS-driven dog tracker service daemon."""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._nc = None
        self._running = True
        self._pipeline = None
        self._pipeline_task = None
        self._ptz = None
        self._commands = 0

    async def start(self):
        nats_cfg = self._cfg["nats"]
        logger.info("Connecting to NATS at %s", nats_cfg["url"])
        self._nc = await nats.connect(
            nats_cfg["url"],
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        js = self._nc.jetstream()

        sub = await js.subscribe(
            nats_cfg["inbox"],
            stream=nats_cfg["stream"],
            config=ConsumerConfig(
                durable_name=nats_cfg["consumer"],
                deliver_policy=DeliverPolicy.NEW,
                ack_policy=AckPolicy.EXPLICIT,
                filter_subject=nats_cfg["inbox"],
            ),
        )

        logger.info("Dog tracker service started — listening on %s", nats_cfg["inbox"])

        # Auto-start pipeline in IDLE mode (2 fps scanning)
        auto_start = await self._start_tracking()
        logger.info("Auto-start: %s", auto_start)

        try:
            async for msg in sub.messages:
                if not self._running:
                    break
                await self._handle(msg)
        except asyncio.CancelledError:
            pass
        finally:
            if self._pipeline:
                self._pipeline.stop()
            if self._pipeline_task:
                self._pipeline_task.cancel()
            await sub.unsubscribe()
            await self._nc.close()
            logger.info("Dog tracker service stopped (commands=%d)", self._commands)

    async def _handle(self, msg):
        await msg.ack()

        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        text = (payload.get("message", "") or payload.get("text", "")).strip().lower()
        if not text:
            return

        from_agent = payload.get("from", "unknown")
        logger.info("Command from %s: %s", from_agent, text)

        response = await self._execute(text)
        self._commands += 1
        logger.info("Result: %s", response)

        # Reply to sender
        try:
            reply_subject = f"agents.{from_agent}.inbox"
            reply_payload = json.dumps({
                "type": "agent_message",
                "from": "dogtracker",
                "message": response,
            })
            await self._nc.publish(reply_subject, reply_payload.encode())
            await self._nc.flush()
        except Exception as e:
            logger.error("Failed to reply to %s: %s", from_agent, e)

    async def _execute(self, text: str) -> str:
        """Parse and execute a command."""
        if text in ("status", "state"):
            return self._get_status()

        if text in ("start", "track", "begin"):
            return await self._start_tracking()

        if text in ("stop", "halt", "pause"):
            return self._stop_tracking()

        if text in ("home", "reset", "center"):
            return self._goto_home()

        return (
            f"Unknown command: '{text}'. "
            "Available: start, stop, status, home"
        )

    def _get_status(self) -> str:
        if self._pipeline:
            stats = self._pipeline.stats
            return (
                f"State: {stats['state']} | "
                f"Frames: {stats['frames_processed']} | "
                f"Detections: {stats['detections_count']} | "
                f"Track ID: {stats['current_track_id']}"
            )
        return "State: OFFLINE | Pipeline not running"

    async def _start_tracking(self) -> str:
        if self._pipeline is not None and self._pipeline._running:
            return f"Already running — {self._pipeline.state.value}"

        cam_cfg = self._cfg["camera"]
        if not cam_cfg["rtsp_url"]:
            return "Error: RTSP URL not configured. Set camera.rtsp_url in config.yaml or RTSP_URL env var."

        # Initialize PTZ if configured
        self._ptz = PTZController(
            host=cam_cfg["onvif_host"],
            port=cam_cfg["onvif_port"],
            user=cam_cfg["onvif_user"],
            password=cam_cfg["onvif_password"],
            max_rate_hz=self._cfg["tracking"]["ptz_command_rate_hz"],
        )

        if cam_cfg["onvif_host"]:
            try:
                self._ptz.connect()
            except Exception as e:
                logger.warning("ONVIF connect failed (tracking without PTZ): %s", e)

        self._pipeline = TrackingPipeline(self._cfg, self._ptz)

        async def event_callback(text):
            await self._announce(text)

        self._pipeline_task = asyncio.create_task(
            self._pipeline.run(event_callback=event_callback)
        )
        return "Dog tracker started — monitoring RTSP feed"

    def _stop_tracking(self) -> str:
        if self._pipeline:
            self._pipeline.stop()
            self._pipeline = None
            if self._pipeline_task:
                self._pipeline_task.cancel()
                self._pipeline_task = None
            return "Dog tracker stopped"
        return "Tracker not running"

    def _goto_home(self) -> str:
        if self._ptz and self._ptz.connected:
            self._ptz.stop()
            self._ptz.goto_home()
            return "Camera returning to home position"
        return "PTZ not connected"

    async def _announce(self, text: str):
        """Send announcement via NATS to speaker service."""
        try:
            payload = json.dumps({
                "type": "agent_message",
                "from": "dogtracker",
                "message": text,
            })
            await self._nc.publish("agents.speaker.inbox", payload.encode())
            await self._nc.flush()
            logger.info("Announced: %s", text)
        except Exception as e:
            logger.error("Announcement failed: %s", e)

    def stop(self):
        self._running = False
        if self._pipeline:
            self._pipeline.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Dog Tracker — YOLO + PTZ tracking via NATS")
    parser.add_argument("--config", help="Path to config.yaml", default=DEFAULT_CONFIG)
    parser.add_argument("--once", metavar="CMD", help="Execute a single command and exit (start, stop, status, home)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logging.getLogger().setLevel(getattr(logging, cfg.get("log_level", "INFO")))

    if args.once:
        service = DogTrackerService(cfg)
        result = asyncio.run(service._execute(args.once))
        print(result)
        return

    service = DogTrackerService(cfg)
    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        service.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run_until_complete(service.start())


if __name__ == "__main__":
    main()
