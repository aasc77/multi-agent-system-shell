"""Tests for orchestrator/providers/voice.py — voice-understanding adapters.

Covers the shared ``understand(audio_wav, system_prompt, allowed_tools)``
contract across Voxtral / Phi-4 Multimodal (unified one-hop backends),
Whisper+LLM (two-hop split backend), and the in-process null stub. Also
covers factory dispatch and config-error surfacing.

Network is never touched — ``requests.post`` is patched in every test
that exercises a remote backend.

Requirements traced to issue #16:
  - Shared Python signature across backends (swap = config change only)
  - HTTP contract: multipart /understand -> {text, tool_call?, latency_ms}
  - whisper_llm matches the same Python signature via client-side chaining
  - Null provider for tests and offline development
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from orchestrator.providers.voice import (
    NullVoiceProvider,
    Phi4MMProvider,
    VoiceProvider,
    VoiceProviderError,
    VoiceResponse,
    VoxtralProvider,
    WhisperLLMProvider,
    get_voice_provider,
)


WAV_BYTES = b"RIFF0000WAVEfmt stub-audio-payload"


def _mock_post_response(payload: dict, status_code: int = 200) -> MagicMock:
    """Build a MagicMock that mimics a ``requests.Response`` JSON result."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Null provider
# ---------------------------------------------------------------------------


class TestNullProvider:
    def test_returns_fixed_text_without_network(self):
        p = NullVoiceProvider(text="hello from null")
        result = p.understand(WAV_BYTES, system_prompt="ignored", allowed_tools=[{"x": 1}])
        assert result.text == "hello from null"
        assert result.tool_call is None
        assert result.latency_ms == 0

    def test_default_text_is_diagnostic(self):
        p = NullVoiceProvider()
        assert "null-voice" in p.understand(b"").text

    def test_can_emit_structured_tool_call(self):
        call = {"name": "hassio.light", "arguments": {"entity_id": "light.kitchen"}}
        p = NullVoiceProvider(text="turn off kitchen lights", tool_call=call)
        result = p.understand(WAV_BYTES)
        assert result.tool_call == call

    def test_satisfies_voice_provider_protocol(self):
        # runtime_checkable Protocol — isinstance() works against duck-typed class
        assert isinstance(NullVoiceProvider(), VoiceProvider)


# ---------------------------------------------------------------------------
# Unified backends (Voxtral / Phi-4 Multimodal)
# ---------------------------------------------------------------------------


class TestUnifiedBackends:
    def test_voxtral_posts_multipart_and_parses_response(self):
        p = VoxtralProvider("http://voxtral.example.com:5100/")
        payload = {"text": "turn on the lamp", "tool_call": None, "latency_ms": 120}

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = _mock_post_response(payload)
            result = p.understand(
                WAV_BYTES,
                system_prompt="you are a home assistant",
                allowed_tools=[{"name": "light"}],
            )

        # Trailing slash must have been stripped
        args, kwargs = mock_post.call_args
        assert kwargs["timeout"] == 30
        assert args[0] == "http://voxtral.example.com:5100/understand"

        # Multipart: file attached under 'file', optional fields under 'data'
        files = kwargs["files"]
        assert "file" in files
        filename, body, mimetype = files["file"]
        assert filename == "audio.wav"
        assert body == WAV_BYTES
        assert mimetype == "audio/wav"

        data = kwargs["data"]
        assert data["system_prompt"] == "you are a home assistant"
        assert json.loads(data["allowed_tools"]) == [{"name": "light"}]

        assert result.text == "turn on the lamp"
        assert result.tool_call is None
        assert result.latency_ms == 120

    def test_omits_optional_fields_when_not_provided(self):
        p = VoxtralProvider("http://voxtral.example.com:5100")
        payload = {"text": "hi", "tool_call": None}

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = _mock_post_response(payload)
            p.understand(WAV_BYTES)

        data = mock_post.call_args.kwargs["data"]
        # When no system prompt / tools are passed, the adapter must NOT send
        # empty-string form fields -- some servers reject them.
        assert "system_prompt" not in data
        assert "allowed_tools" not in data

    def test_falls_back_to_wallclock_when_server_omits_latency(self):
        p = VoxtralProvider("http://voxtral.example.com:5100")
        payload = {"text": "ok"}  # no latency_ms field

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = _mock_post_response(payload)
            result = p.understand(WAV_BYTES)

        assert result.latency_ms >= 0  # wallclock fallback populated

    def test_parses_tool_call_passthrough(self):
        p = VoxtralProvider("http://voxtral.example.com:5100")
        tc = {"name": "hassio.light", "arguments": {"entity_id": "light.x", "state": "on"}}
        payload = {"text": "turn on light x", "tool_call": tc, "latency_ms": 310}

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = _mock_post_response(payload)
            result = p.understand(WAV_BYTES)

        assert result.tool_call == tc

    def test_phi4mm_uses_same_contract_as_voxtral(self):
        p = Phi4MMProvider("http://phi4.example.com:5100")
        payload = {"text": "same contract", "tool_call": None, "latency_ms": 50}

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = _mock_post_response(payload)
            result = p.understand(WAV_BYTES)

        assert mock_post.call_args.args[0] == "http://phi4.example.com:5100/understand"
        assert result.text == "same contract"

    def test_http_error_surfaces_as_provider_error(self):
        p = VoxtralProvider("http://voxtral.example.com:5100")

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError("boom")
            with pytest.raises(VoiceProviderError) as excinfo:
                p.understand(WAV_BYTES)

        assert "voxtral" in str(excinfo.value)
        assert "boom" in str(excinfo.value)

    def test_non_json_response_surfaces_as_provider_error(self):
        p = VoxtralProvider("http://voxtral.example.com:5100")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.side_effect = ValueError("not json")

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = resp
            with pytest.raises(VoiceProviderError):
                p.understand(WAV_BYTES)

    def test_missing_text_field_rejects_response(self):
        p = VoxtralProvider("http://voxtral.example.com:5100")
        payload = {"tool_call": None}  # no 'text' field

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = _mock_post_response(payload)
            with pytest.raises(VoiceProviderError):
                p.understand(WAV_BYTES)

    def test_tool_call_must_be_dict_or_null(self):
        p = VoxtralProvider("http://voxtral.example.com:5100")
        payload = {"text": "x", "tool_call": ["not", "a", "dict"]}

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = _mock_post_response(payload)
            with pytest.raises(VoiceProviderError):
                p.understand(WAV_BYTES)

    def test_respects_per_backend_timeout_override(self):
        p = VoxtralProvider("http://voxtral.example.com:5100", timeout_seconds=5)
        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.return_value = _mock_post_response({"text": "ok"})
            p.understand(WAV_BYTES)
        assert mock_post.call_args.kwargs["timeout"] == 5


# ---------------------------------------------------------------------------
# Split backend (Whisper STT + OpenAI-compatible chat completions)
# ---------------------------------------------------------------------------


class TestWhisperLLMProvider:
    def _stt_response(self, text: str = "hello world"):
        return _mock_post_response({"text": text})

    def _chat_response(
        self,
        content: str = "reply",
        tool_calls: list | None = None,
    ):
        message = {"role": "assistant", "content": content}
        if tool_calls is not None:
            message["tool_calls"] = tool_calls
        return _mock_post_response({"choices": [{"message": message}]})

    def test_chains_stt_then_llm_and_returns_llm_content(self):
        p = WhisperLLMProvider(
            stt_url="http://stt.example/",
            llm_url="http://llm.example/",
            model="llama-3-8b",
        )

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.side_effect = [
                self._stt_response("turn off lights"),
                self._chat_response("OK, turning off the lights"),
            ]
            result = p.understand(
                WAV_BYTES,
                system_prompt="you are a home assistant",
                allowed_tools=[{"type": "function", "function": {"name": "light"}}],
            )

        assert mock_post.call_count == 2

        # First call: STT /transcribe with audio file
        stt_call = mock_post.call_args_list[0]
        assert stt_call.args[0] == "http://stt.example/transcribe"
        assert "file" in stt_call.kwargs["files"]

        # Second call: LLM /v1/chat/completions with JSON body
        llm_call = mock_post.call_args_list[1]
        assert llm_call.args[0] == "http://llm.example/v1/chat/completions"
        body = llm_call.kwargs["json"]
        assert body["model"] == "llama-3-8b"
        assert body["messages"][0] == {"role": "system", "content": "you are a home assistant"}
        assert body["messages"][1] == {"role": "user", "content": "turn off lights"}
        assert body["tools"] == [{"type": "function", "function": {"name": "light"}}]

        assert result.text == "OK, turning off the lights"
        assert result.tool_call is None
        assert result.raw["transcript"] == "turn off lights"

    def test_decodes_openai_tool_call_arguments_string(self):
        p = WhisperLLMProvider(
            stt_url="http://stt", llm_url="http://llm", model="m"
        )
        tool_calls = [{
            "function": {
                "name": "hassio.light",
                "arguments": json.dumps({"entity_id": "light.kitchen", "state": "off"}),
            }
        }]

        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.side_effect = [
                self._stt_response("turn off kitchen lights"),
                self._chat_response(content="calling tool", tool_calls=tool_calls),
            ]
            result = p.understand(WAV_BYTES)

        assert result.tool_call == {
            "name": "hassio.light",
            "arguments": {"entity_id": "light.kitchen", "state": "off"},
        }

    def test_stt_failure_surfaces_as_provider_error(self):
        p = WhisperLLMProvider(stt_url="http://stt", llm_url="http://llm", model="m")
        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError("stt down")
            with pytest.raises(VoiceProviderError) as excinfo:
                p.understand(WAV_BYTES)
        assert "STT" in str(excinfo.value) or "stt" in str(excinfo.value)

    def test_llm_failure_after_successful_stt_surfaces_as_provider_error(self):
        p = WhisperLLMProvider(stt_url="http://stt", llm_url="http://llm", model="m")
        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.side_effect = [
                self._stt_response("hi"),
                requests.exceptions.ConnectionError("llm down"),
            ]
            with pytest.raises(VoiceProviderError) as excinfo:
                p.understand(WAV_BYTES)
        assert "LLM" in str(excinfo.value) or "llm" in str(excinfo.value)

    def test_empty_choices_rejected(self):
        p = WhisperLLMProvider(stt_url="http://stt", llm_url="http://llm", model="m")
        with patch("orchestrator.providers.voice.requests.post") as mock_post:
            mock_post.side_effect = [
                self._stt_response("hi"),
                _mock_post_response({"choices": []}),
            ]
            with pytest.raises(VoiceProviderError):
                p.understand(WAV_BYTES)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestGetVoiceProvider:
    def test_voxtral_backend_returns_voxtral_provider(self):
        cfg = {"backend": "voxtral", "voxtral": {"url": "http://v:5100"}}
        p = get_voice_provider(cfg)
        assert isinstance(p, VoxtralProvider)

    def test_phi4_backend_returns_phi4_provider(self):
        cfg = {"backend": "phi4_multimodal", "phi4_multimodal": {"url": "http://p:5100"}}
        p = get_voice_provider(cfg)
        assert isinstance(p, Phi4MMProvider)

    def test_whisper_llm_backend_returns_chained_provider(self):
        cfg = {
            "backend": "whisper_llm",
            "whisper_llm": {
                "stt_url": "http://stt",
                "llm_url": "http://llm",
                "model": "m",
            },
        }
        p = get_voice_provider(cfg)
        assert isinstance(p, WhisperLLMProvider)

    def test_null_backend_returns_null_provider_without_subsection(self):
        cfg = {"backend": "null"}
        p = get_voice_provider(cfg)
        assert isinstance(p, NullVoiceProvider)

    def test_null_backend_respects_configured_text(self):
        cfg = {"backend": "null", "null": {"text": "offline stub"}}
        p = get_voice_provider(cfg)
        assert p.understand(b"").text == "offline stub"

    def test_missing_backend_raises(self):
        with pytest.raises(VoiceProviderError):
            get_voice_provider({})

    def test_unknown_backend_raises_with_supported_list(self):
        with pytest.raises(VoiceProviderError) as excinfo:
            get_voice_provider({"backend": "chatterbox"})
        msg = str(excinfo.value)
        for supported in ("voxtral", "whisper_llm", "phi4_multimodal", "null"):
            assert supported in msg

    def test_non_dict_config_raises(self):
        with pytest.raises(VoiceProviderError):
            get_voice_provider("voxtral")  # type: ignore[arg-type]

    def test_voxtral_missing_url_raises_clearly(self):
        with pytest.raises(VoiceProviderError) as excinfo:
            get_voice_provider({"backend": "voxtral", "voxtral": {}})
        assert "url" in str(excinfo.value)

    def test_voxtral_missing_subsection_raises(self):
        with pytest.raises(VoiceProviderError) as excinfo:
            get_voice_provider({"backend": "voxtral"})
        assert "voxtral" in str(excinfo.value)

    def test_whisper_llm_requires_all_three_fields(self):
        # Missing model
        with pytest.raises(VoiceProviderError) as excinfo:
            get_voice_provider({
                "backend": "whisper_llm",
                "whisper_llm": {"stt_url": "http://stt", "llm_url": "http://llm"},
            })
        assert "model" in str(excinfo.value)

        # Missing stt_url
        with pytest.raises(VoiceProviderError):
            get_voice_provider({
                "backend": "whisper_llm",
                "whisper_llm": {"llm_url": "http://llm", "model": "m"},
            })

        # Missing llm_url
        with pytest.raises(VoiceProviderError):
            get_voice_provider({
                "backend": "whisper_llm",
                "whisper_llm": {"stt_url": "http://stt", "model": "m"},
            })


# ---------------------------------------------------------------------------
# VoiceResponse dataclass
# ---------------------------------------------------------------------------


class TestVoiceResponse:
    def test_defaults_for_plain_transcript(self):
        r = VoiceResponse(text="hello")
        assert r.tool_call is None
        assert r.latency_ms == 0
        assert r.raw == {}
