"""Voice-understanding provider adapter (issue #16).

Single Python entry point — ``understand(audio_wav, system_prompt, allowed_tools)``
— that dispatches to a pluggable backend based on ``providers.voice.backend``
in the merged config. Backends implement the same signature so swapping
models (Voxtral → Phi-4 Multimodal → Whisper+LLM split → ``null`` for tests)
is a one-line config edit, not a code change.

Config shape::

    providers:
      voice:
        backend: voxtral | whisper_llm | phi4_multimodal | null
        voxtral:
          url: http://...
          timeout_seconds: 30
        whisper_llm:
          stt_url: http://...              # existing /transcribe endpoint
          llm_url: http://...              # OpenAI-compatible /v1/chat/completions
          model: llama-3-8b
          timeout_seconds: 30
        phi4_multimodal:
          url: http://...
          timeout_seconds: 30
        # backend: null needs no sub-section

HTTP contract for unified backends (voxtral, phi4_multimodal)::

    POST {url}/understand  (multipart/form-data)
        file           = <wav 16kHz mono bytes>
        system_prompt  = <str>   (optional)
        allowed_tools  = <json>  (optional, serialized list[dict])

    200 -> {
        "text":       <str>,
        "tool_call":  {"name": str, "arguments": dict} | null,
        "latency_ms": <int>,
    }

``whisper_llm`` does NOT expose this endpoint — it implements the same
Python contract client-side by chaining two existing HTTP calls (Whisper
``/transcribe`` + OpenAI-compatible ``/v1/chat/completions``).

Usage::

    from orchestrator.providers.voice import get_voice_provider
    provider = get_voice_provider(cfg["providers"]["voice"])
    result = provider.understand(wav_bytes, system_prompt="...", allowed_tools=[...])
    print(result.text, result.tool_call)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import requests

__all__ = [
    "NullVoiceProvider",
    "Phi4MMProvider",
    "VoiceProvider",
    "VoiceProviderError",
    "VoiceResponse",
    "VoxtralProvider",
    "WhisperLLMProvider",
    "get_voice_provider",
]

DEFAULT_TIMEOUT_SECONDS = 30
SUPPORTED_BACKENDS = ("voxtral", "whisper_llm", "phi4_multimodal", "null")


class VoiceProviderError(Exception):
    """Raised when a voice-understanding request fails or is misconfigured."""


@dataclass
class VoiceResponse:
    """Result of a single ``understand()`` call.

    ``tool_call`` is ``None`` when the backend returned a plain transcript
    with no structured tool invocation. ``latency_ms`` is measured
    end-to-end by the adapter (not reported by the remote server) so
    operators get a consistent latency signal across backends.
    """

    text: str
    tool_call: dict[str, Any] | None = None
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VoiceProvider(Protocol):
    """Python contract every voice backend must satisfy.

    Implementations should be side-effect-free constructors (store
    endpoint URLs etc.) and do all I/O inside ``understand``.
    """

    def understand(
        self,
        audio_wav: bytes,
        system_prompt: str = "",
        allowed_tools: list[dict[str, Any]] | None = None,
    ) -> VoiceResponse:
        ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_url(cfg: dict[str, Any], backend: str, key: str = "url") -> str:
    """Return ``cfg[key]`` or raise a clear config error."""
    value = cfg.get(key)
    if not value or not isinstance(value, str):
        raise VoiceProviderError(
            f"providers.voice.{backend}.{key} is required (got {value!r})"
        )
    return value


def _timeout(cfg: dict[str, Any]) -> int:
    """Per-backend timeout override with a safe default."""
    return int(cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))


def _parse_understand_response(payload: dict[str, Any], elapsed_ms: int) -> VoiceResponse:
    """Validate and wrap an ``/understand`` response envelope."""
    text = payload.get("text")
    if not isinstance(text, str):
        raise VoiceProviderError(
            f"Provider response missing string 'text' field: {payload!r}"
        )
    tool_call = payload.get("tool_call")
    if tool_call is not None and not isinstance(tool_call, dict):
        raise VoiceProviderError(
            f"Provider 'tool_call' must be dict or null, got {type(tool_call).__name__}"
        )
    # Prefer the server-reported latency when present; otherwise fall back
    # to the adapter-measured wall clock so ops always get a number.
    reported = payload.get("latency_ms")
    latency = int(reported) if isinstance(reported, (int, float)) else elapsed_ms
    return VoiceResponse(text=text.strip(), tool_call=tool_call, latency_ms=latency, raw=payload)


# ---------------------------------------------------------------------------
# Unified backends: one HTTP call, audio → text + optional tool_call
# ---------------------------------------------------------------------------


class _UnifiedUnderstandProvider:
    """Shared implementation for backends that expose POST /understand.

    Voxtral and Phi-4 Multimodal are concrete subclasses that exist only to
    give operators a recognisable backend name in the config; the wire
    contract is identical.
    """

    backend_name: str = "unified"

    def __init__(self, url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._url = url.rstrip("/")
        self._timeout = timeout_seconds

    def understand(
        self,
        audio_wav: bytes,
        system_prompt: str = "",
        allowed_tools: list[dict[str, Any]] | None = None,
    ) -> VoiceResponse:
        data: dict[str, str] = {}
        if system_prompt:
            data["system_prompt"] = system_prompt
        if allowed_tools is not None:
            data["allowed_tools"] = json.dumps(allowed_tools)

        started = time.perf_counter()
        try:
            resp = requests.post(
                f"{self._url}/understand",
                files={"file": ("audio.wav", audio_wav, "audio/wav")},
                data=data,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            raise VoiceProviderError(
                f"{self.backend_name}: request to {self._url}/understand failed: {e}"
            ) from e
        except ValueError as e:
            raise VoiceProviderError(
                f"{self.backend_name}: non-JSON response from {self._url}/understand"
            ) from e

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return _parse_understand_response(payload, elapsed_ms)


class VoxtralProvider(_UnifiedUnderstandProvider):
    """Voxtral-Small-24B adapter (audio → text + optional tool_call in one hop)."""

    backend_name = "voxtral"


class Phi4MMProvider(_UnifiedUnderstandProvider):
    """Phi-4 Multimodal adapter. Same HTTP contract as Voxtral."""

    backend_name = "phi4_multimodal"


# ---------------------------------------------------------------------------
# Split backend: Whisper STT → OpenAI-compatible chat completions
# ---------------------------------------------------------------------------


class WhisperLLMProvider:
    """Two-hop adapter chaining Whisper transcription + an LLM chat call.

    Exists so swapping between a unified model (Voxtral) and a classic
    split stack is a config change only. The LLM endpoint must accept
    OpenAI-style ``/v1/chat/completions`` payloads (vLLM, llama.cpp server,
    LM Studio, and Ollama's OpenAI shim all do).
    """

    backend_name = "whisper_llm"

    def __init__(
        self,
        stt_url: str,
        llm_url: str,
        model: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._stt_url = stt_url.rstrip("/")
        self._llm_url = llm_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    def _transcribe(self, audio_wav: bytes) -> str:
        try:
            resp = requests.post(
                f"{self._stt_url}/transcribe",
                files={"file": ("audio.wav", audio_wav, "audio/wav")},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            raise VoiceProviderError(
                f"whisper_llm: STT request to {self._stt_url}/transcribe failed: {e}"
            ) from e
        except ValueError as e:
            raise VoiceProviderError(
                f"whisper_llm: non-JSON STT response from {self._stt_url}"
            ) from e

        text = payload.get("text")
        if not isinstance(text, str):
            raise VoiceProviderError(
                f"whisper_llm: STT response missing 'text' field: {payload!r}"
            )
        return text.strip()

    def _chat(
        self,
        transcript: str,
        system_prompt: str,
        allowed_tools: list[dict[str, Any]] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": transcript})

        body: dict[str, Any] = {"model": self._model, "messages": messages}
        if allowed_tools:
            body["tools"] = allowed_tools

        try:
            resp = requests.post(
                f"{self._llm_url}/v1/chat/completions",
                json=body,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            raise VoiceProviderError(
                f"whisper_llm: LLM request to {self._llm_url} failed: {e}"
            ) from e
        except ValueError as e:
            raise VoiceProviderError(
                f"whisper_llm: non-JSON LLM response from {self._llm_url}"
            ) from e

        choices = payload.get("choices") or []
        if not choices:
            raise VoiceProviderError(f"whisper_llm: LLM returned no choices: {payload!r}")

        message = choices[0].get("message") or {}
        content = message.get("content") or transcript

        tool_call: dict[str, Any] | None = None
        tc_list = message.get("tool_calls")
        if tc_list:
            first = tc_list[0] or {}
            fn = first.get("function") or {}
            args = fn.get("arguments")
            # OpenAI returns arguments as a JSON-encoded string; decode so
            # callers get a uniform dict regardless of backend.
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tool_call = {"name": fn.get("name", ""), "arguments": args or {}}

        return content.strip() if isinstance(content, str) else transcript, tool_call

    def understand(
        self,
        audio_wav: bytes,
        system_prompt: str = "",
        allowed_tools: list[dict[str, Any]] | None = None,
    ) -> VoiceResponse:
        started = time.perf_counter()
        transcript = self._transcribe(audio_wav)
        text, tool_call = self._chat(transcript, system_prompt, allowed_tools)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return VoiceResponse(
            text=text,
            tool_call=tool_call,
            latency_ms=elapsed_ms,
            raw={"transcript": transcript},
        )


# ---------------------------------------------------------------------------
# Null backend: in-process stub for tests and offline development
# ---------------------------------------------------------------------------


class NullVoiceProvider:
    """In-process stub. Returns a fixed transcript; never hits the network.

    Use in unit tests and when operators want the voice pipeline wired up
    but every live endpoint is offline (CI, plane-mode laptop, etc).
    """

    backend_name = "null"

    def __init__(
        self,
        text: str = "[null-voice] no backend configured",
        tool_call: dict[str, Any] | None = None,
    ) -> None:
        self._text = text
        self._tool_call = tool_call

    def understand(
        self,
        audio_wav: bytes,
        system_prompt: str = "",
        allowed_tools: list[dict[str, Any]] | None = None,
    ) -> VoiceResponse:
        return VoiceResponse(text=self._text, tool_call=self._tool_call, latency_ms=0)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_voice_provider(voice_cfg: dict[str, Any]) -> VoiceProvider:
    """Return the configured voice provider instance.

    Args:
        voice_cfg: The ``providers.voice`` subtree from the merged config,
            as a plain ``dict`` (use ``vars()`` on a ConfigNode if needed).

    Raises:
        VoiceProviderError: If ``backend`` is missing or unknown, or if the
            selected backend's required sub-fields are missing.
    """
    if not isinstance(voice_cfg, dict):
        raise VoiceProviderError(
            f"providers.voice must be a mapping, got {type(voice_cfg).__name__}"
        )

    backend = voice_cfg.get("backend")
    if not backend:
        raise VoiceProviderError("providers.voice.backend is required")
    if backend not in SUPPORTED_BACKENDS:
        raise VoiceProviderError(
            f"Unknown voice backend {backend!r}. "
            f"Supported: {', '.join(SUPPORTED_BACKENDS)}"
        )

    if backend == "null":
        null_cfg = voice_cfg.get("null") or {}
        return NullVoiceProvider(
            text=null_cfg.get("text", "[null-voice] no backend configured"),
            tool_call=null_cfg.get("tool_call"),
        )

    sub = voice_cfg.get(backend)
    if not isinstance(sub, dict):
        raise VoiceProviderError(
            f"providers.voice.{backend} sub-section missing or not a mapping"
        )

    if backend == "voxtral":
        return VoxtralProvider(_require_url(sub, backend), _timeout(sub))

    if backend == "phi4_multimodal":
        return Phi4MMProvider(_require_url(sub, backend), _timeout(sub))

    if backend == "whisper_llm":
        stt_url = _require_url(sub, backend, "stt_url")
        llm_url = _require_url(sub, backend, "llm_url")
        model = sub.get("model")
        if not model or not isinstance(model, str):
            raise VoiceProviderError(
                "providers.voice.whisper_llm.model is required (e.g. 'llama-3-8b')"
            )
        return WhisperLLMProvider(stt_url, llm_url, model, _timeout(sub))

    # Unreachable — SUPPORTED_BACKENDS is exhausted above.
    raise VoiceProviderError(f"No factory branch for backend {backend!r}")
