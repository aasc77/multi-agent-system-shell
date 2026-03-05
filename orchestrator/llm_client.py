"""Ollama LLM Client for the Multi-Agent System Shell.

Optional LLM integration for routing decisions and command interpretation.

Requirements traced to PRD:
  - R10: LLM Client (Ollama) -- health check, config settings, graceful degradation
  - R9: Configuration -- llm config section (provider, model, base_url, temperature)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_DISABLE_THINKING = False
_API_GENERATE_PATH = "/api/generate"
_HTTP_OK = 200

# --- Log message templates ---
_LOG_HEALTH_NON_200 = "LLM health check failed: Ollama returned status %d"
_LOG_HEALTH_UNREACHABLE = (
    "LLM health check failed: Ollama unreachable at %s - %s"
)
_LOG_QUERY_SKIPPED = "LLM query skipped: LLM is not available / unavailable"
_LOG_QUERY_FAILED = "LLM query failed: status %d - %s"
_LOG_QUERY_ERROR = "LLM query error: %s"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMClientError(Exception):
    """Raised when the LLM client encounters an error."""


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Ollama LLM client for optional routing and command interpretation.

    Args:
        config: Dict with LLM settings (provider, model, base_url, temperature,
                disable_thinking).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._provider: str = config["provider"]
        self._model: str = config["model"]
        self._base_url: str = config.get("base_url", _DEFAULT_BASE_URL)
        self._temperature: float = config.get("temperature", _DEFAULT_TEMPERATURE)
        self._disable_thinking: bool = config.get(
            "disable_thinking", _DEFAULT_DISABLE_THINKING
        )
        self._is_available = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def provider(self) -> str:
        """Return the configured provider."""
        return self._provider

    @property
    def model(self) -> str:
        """Return the configured model."""
        return self._model

    @property
    def base_url(self) -> str:
        """Return the configured base URL."""
        return self._base_url

    @property
    def temperature(self) -> float:
        """Return the configured temperature."""
        return self._temperature

    @property
    def disable_thinking(self) -> bool:
        """Return the configured disable_thinking setting."""
        return self._disable_thinking

    @property
    def is_available(self) -> bool:
        """Return whether the LLM is available (health check passed)."""
        return self._is_available

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check if the Ollama server is reachable.

        Non-fatal: returns ``True`` if reachable, ``False`` otherwise.
        Logs a warning if unreachable. Never raises.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(self._base_url)
                if response.status_code == _HTTP_OK:
                    self._is_available = True
                    return True
                self._is_available = False
                logger.warning(_LOG_HEALTH_NON_200, response.status_code)
                return False
        except Exception as exc:
            self._is_available = False
            logger.warning(_LOG_HEALTH_UNREACHABLE, self._base_url, exc)
            return False

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    async def query(self, prompt: str) -> str | None:
        """Send a prompt to the Ollama API and return the response.

        Returns ``None`` if the LLM is unavailable or an error occurs.
        """
        if not self._is_available:
            logger.warning(_LOG_QUERY_SKIPPED)
            return None

        try:
            url = f"{self._base_url}{_API_GENERATE_PATH}"
            payload = self._build_query_payload(prompt)

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload)

            if response.status_code != _HTTP_OK:
                logger.warning(_LOG_QUERY_FAILED, response.status_code, response.text)
                return None

            data = response.json()
            return data.get("response", "")

        except Exception as exc:
            logger.warning(_LOG_QUERY_ERROR, exc)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_query_payload(self, prompt: str) -> dict[str, Any]:
        """Build the JSON payload for an Ollama ``/api/generate`` request."""
        return {
            "model": self._model,
            "prompt": prompt,
            "temperature": self._temperature,
            "stream": False,
        }
