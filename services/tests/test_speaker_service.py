"""
Tests for speaker service — voice resolution, message parsing, routing.

Usage:
    cd /path/to/multi-agent-system-shell
    python3 -m pytest services/tests/test_speaker_service.py -v
"""

import json
import os
import sys

import pytest

# Add services to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from importlib import import_module

speaker = import_module("speaker-service")


# ---------------------------------------------------------------------------
# Tests: resolve_voice
# ---------------------------------------------------------------------------

class TestResolveVoice:
    def test_known_agents(self):
        assert speaker.resolve_voice("hub") == "lessac"
        assert speaker.resolve_voice("dgx") == "ryan"
        assert speaker.resolve_voice("dgx2") == "ryan"
        assert speaker.resolve_voice("macmini") == "kusal"
        assert speaker.resolve_voice("hassio") == "amy"
        assert speaker.resolve_voice("manager") == "alba"

    def test_unknown_agent_gets_default(self):
        assert speaker.resolve_voice("unknown") == speaker.DEFAULT_VOICE
        assert speaker.resolve_voice("newagent") == speaker.DEFAULT_VOICE

    def test_orchestrator_and_system(self):
        assert speaker.resolve_voice("orchestrator") == "amy"
        assert speaker.resolve_voice("system") == "amy"


# ---------------------------------------------------------------------------
# Tests: parse_speak_request
# ---------------------------------------------------------------------------

class TestParseSpeakRequest:
    def test_mcp_bridge_format(self):
        """MCP bridge sends messages with 'message' field."""
        data = json.dumps({
            "type": "agent_message",
            "from": "hub",
            "message": "Deployment complete",
        }).encode()
        result = speaker.parse_speak_request(data)
        assert result == {"text": "Deployment complete", "from": "hub"}

    def test_direct_format(self):
        """Direct publish uses 'text' field."""
        data = json.dumps({
            "text": "Hello world",
            "from": "manager",
        }).encode()
        result = speaker.parse_speak_request(data)
        assert result == {"text": "Hello world", "from": "manager"}

    def test_message_field_takes_priority(self):
        """If both 'message' and 'text' present, 'message' wins."""
        data = json.dumps({
            "message": "Primary",
            "text": "Fallback",
            "from": "hub",
        }).encode()
        result = speaker.parse_speak_request(data)
        assert result["text"] == "Primary"

    def test_missing_from_defaults_to_unknown(self):
        data = json.dumps({"message": "No sender"}).encode()
        result = speaker.parse_speak_request(data)
        assert result["from"] == "unknown"

    def test_empty_message_returns_none(self):
        data = json.dumps({"from": "hub", "message": ""}).encode()
        result = speaker.parse_speak_request(data)
        assert result is None

    def test_no_text_fields_returns_none(self):
        data = json.dumps({"from": "hub", "status": "pass"}).encode()
        result = speaker.parse_speak_request(data)
        assert result is None

    def test_invalid_json_returns_none(self):
        result = speaker.parse_speak_request(b"not json")
        assert result is None

    def test_binary_garbage_returns_none(self):
        result = speaker.parse_speak_request(b"\x00\xff\xfe")
        assert result is None

    def test_whitespace_stripped(self):
        data = json.dumps({"message": "  hello  ", "from": "hub"}).encode()
        result = speaker.parse_speak_request(data)
        assert result["text"] == "hello"
