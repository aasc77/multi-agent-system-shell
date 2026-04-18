"""Pluggable provider adapters for the MAS runtime.

Each submodule is one provider family (voice understanding, TTS, etc.).
Concrete backends implement a shared Python signature so swapping between
them is a one-line config edit under ``providers.<family>`` in
``config.yaml`` — no caller-side code changes.
"""

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
