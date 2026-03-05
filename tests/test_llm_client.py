"""
Tests for orchestrator/llm_client.py -- Ollama LLM Client

TDD Contract (RED phase):
These tests define the expected behavior of the Ollama LLM Client module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R10: LLM Client (Ollama) -- optional routing, health check, config settings
  - R9: Configuration -- llm config section (provider, model, base_url, temperature)
  - R7: Interactive Orchestrator Console -- LLM for interpreting commands (future)

Acceptance criteria from task rgr-9:
  9. Ollama health check on startup: if unreachable, logs warning and continues without LLM
  10. LLM client respects config settings (provider, model, base_url, temperature)

Test categories:
  1. Construction and config -- accepts llm config, stores settings
  2. Health check -- startup check, non-fatal if unreachable
  3. Config respect -- provider, model, base_url, temperature
  4. Query interface -- send prompt, receive response
  5. Disabled mode -- graceful degradation when LLM unreachable
  6. Edge cases and error handling
"""

import logging
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

# --- The imports that MUST fail in RED phase ---
from orchestrator.llm_client import LLMClient, LLMClientError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DEFAULT_LLM_CONFIG = {
    "provider": "ollama",
    "model": "qwen3:8b",
    "base_url": "http://localhost:11434",
    "temperature": 0.3,
    "disable_thinking": True,
}

CUSTOM_LLM_CONFIG = {
    "provider": "ollama",
    "model": "llama3:latest",
    "base_url": "http://custom-host:11434",
    "temperature": 0.7,
    "disable_thinking": False,
}


@pytest.fixture
def llm_config():
    """Return default LLM config."""
    return DEFAULT_LLM_CONFIG.copy()


@pytest.fixture
def custom_config():
    """Return custom LLM config."""
    return CUSTOM_LLM_CONFIG.copy()


@pytest.fixture
def client(llm_config):
    """Create an LLMClient with default config."""
    return LLMClient(config=llm_config)


# ===========================================================================
# 1. CONSTRUCTION AND CONFIG
# ===========================================================================


class TestConstruction:
    """LLMClient must accept LLM config and store settings."""

    def test_creates_instance(self, client):
        """LLMClient constructor should return a valid instance."""
        assert client is not None

    def test_constructor_accepts_config(self, llm_config):
        """LLMClient should accept a config dict."""
        client = LLMClient(config=llm_config)
        assert client is not None

    def test_llm_client_error_is_exception(self):
        """LLMClientError must be a subclass of Exception."""
        assert issubclass(LLMClientError, Exception)

    def test_llm_client_error_has_message(self):
        """LLMClientError should accept and store a message."""
        err = LLMClientError("test error")
        assert "test error" in str(err)

    def test_stores_provider(self, client):
        """LLMClient must store the provider from config."""
        assert client.provider == "ollama"

    def test_stores_model(self, client):
        """LLMClient must store the model from config."""
        assert client.model == "qwen3:8b"

    def test_stores_base_url(self, client):
        """LLMClient must store the base_url from config."""
        assert client.base_url == "http://localhost:11434"

    def test_stores_temperature(self, client):
        """LLMClient must store the temperature from config."""
        assert client.temperature == 0.3


# ===========================================================================
# 2. HEALTH CHECK -- startup check, non-fatal if unreachable
# ===========================================================================


class TestHealthCheck:
    """Ollama health check on startup: non-fatal if unreachable, logs warning."""

    @pytest.mark.asyncio
    async def test_health_check_succeeds_when_reachable(self, client):
        """health_check must return True when Ollama is reachable."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            result = await client.health_check()
            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_unreachable(self, client):
        """health_check must return False when Ollama is unreachable."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=ConnectionError("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            result = await client.health_check()
            assert result is False

    @pytest.mark.asyncio
    async def test_health_check_does_not_raise_when_unreachable(self, client):
        """health_check must NOT raise an exception -- it's non-fatal."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Network error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            # Must NOT raise
            result = await client.health_check()
            assert result is False

    @pytest.mark.asyncio
    async def test_health_check_logs_warning_when_unreachable(self, client, caplog):
        """health_check must log a warning when Ollama is unreachable."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=ConnectionError("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            with caplog.at_level(logging.WARNING):
                await client.health_check()

            warning_messages = [
                r.message for r in caplog.records if r.levelno >= logging.WARNING
            ]
            assert any(
                "ollama" in msg.lower() or "unreachable" in msg.lower() or "llm" in msg.lower()
                for msg in warning_messages
            ), f"Expected warning about LLM unreachable, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_health_check_sets_available_flag(self, client):
        """health_check must set an is_available flag based on reachability."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            await client.health_check()
            assert client.is_available is True

    @pytest.mark.asyncio
    async def test_health_check_clears_available_flag_on_failure(self, client):
        """health_check must clear is_available when unreachable."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=ConnectionError("fail"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            await client.health_check()
            assert client.is_available is False

    @pytest.mark.asyncio
    async def test_health_check_uses_configured_base_url(self, client):
        """health_check must ping the configured base_url, not a hardcoded one."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            await client.health_check()
            # The get call should target the configured base_url
            call_args = mock_client.get.call_args
            assert "localhost:11434" in str(call_args)

    @pytest.mark.asyncio
    async def test_initially_not_available(self, client):
        """LLMClient should be not available before health_check is called."""
        assert client.is_available is False


# ===========================================================================
# 3. CONFIG RESPECT -- provider, model, base_url, temperature
# ===========================================================================


class TestConfigRespect:
    """LLM client must respect config settings."""

    def test_custom_provider(self, custom_config):
        """LLMClient must respect custom provider setting."""
        client = LLMClient(config=custom_config)
        assert client.provider == "ollama"

    def test_custom_model(self, custom_config):
        """LLMClient must respect custom model setting."""
        client = LLMClient(config=custom_config)
        assert client.model == "llama3:latest"

    def test_custom_base_url(self, custom_config):
        """LLMClient must respect custom base_url setting."""
        client = LLMClient(config=custom_config)
        assert client.base_url == "http://custom-host:11434"

    def test_custom_temperature(self, custom_config):
        """LLMClient must respect custom temperature setting."""
        client = LLMClient(config=custom_config)
        assert client.temperature == 0.7

    def test_disable_thinking_setting(self, llm_config):
        """LLMClient must store the disable_thinking setting."""
        client = LLMClient(config=llm_config)
        assert client.disable_thinking is True

    def test_disable_thinking_false(self, custom_config):
        """LLMClient must respect disable_thinking=False."""
        client = LLMClient(config=custom_config)
        assert client.disable_thinking is False


# ===========================================================================
# 4. QUERY INTERFACE -- send prompt, receive response
# ===========================================================================


class TestQueryInterface:
    """LLMClient must provide a query interface for sending prompts."""

    @pytest.mark.asyncio
    async def test_query_returns_string(self, client):
        """query must return a string response."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "response": "Here is my answer"
            }
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            # Mark as available
            client._is_available = True
            result = await client.query("What is 2+2?")
            assert isinstance(result, str)
            assert "answer" in result.lower() or len(result) > 0

    @pytest.mark.asyncio
    async def test_query_sends_configured_model(self, client):
        """query must send the configured model name in the request."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"response": "ok"}
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            client._is_available = True
            await client.query("test prompt")

            call_args = mock_client.post.call_args
            # The request body should contain the model name
            assert "qwen3:8b" in str(call_args)

    @pytest.mark.asyncio
    async def test_query_sends_temperature(self, client):
        """query must send the configured temperature in the request."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"response": "ok"}
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            client._is_available = True
            await client.query("test prompt")

            call_args = mock_client.post.call_args
            assert "0.3" in str(call_args) or "temperature" in str(call_args)

    @pytest.mark.asyncio
    async def test_query_uses_configured_base_url(self, client):
        """query must send requests to the configured base_url."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"response": "ok"}
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            client._is_available = True
            await client.query("test prompt")

            call_args = mock_client.post.call_args
            assert "localhost:11434" in str(call_args)


# ===========================================================================
# 5. DISABLED MODE -- graceful degradation when LLM unreachable
# ===========================================================================


class TestDisabledMode:
    """When LLM is unreachable, client must degrade gracefully."""

    @pytest.mark.asyncio
    async def test_query_when_unavailable_returns_none(self, client):
        """query when LLM is unavailable must return None (not raise)."""
        client._is_available = False
        result = await client.query("test prompt")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_when_unavailable_logs_warning(self, client, caplog):
        """query when unavailable must log a warning."""
        client._is_available = False
        with caplog.at_level(logging.WARNING):
            await client.query("test prompt")

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unavailable" in msg.lower() or "not available" in msg.lower() or "llm" in msg.lower()
            for msg in warning_messages
        ), f"Expected warning about LLM unavailable, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_query_when_unavailable_does_not_raise(self, client):
        """query when unavailable must NOT raise an exception."""
        client._is_available = False
        # Must not raise
        result = await client.query("any prompt")
        # None is acceptable return value

    def test_is_available_property(self, client):
        """is_available must be a readable property."""
        assert hasattr(client, "is_available")
        assert isinstance(client.is_available, bool)


# ===========================================================================
# 6. EDGE CASES AND ERROR HANDLING
# ===========================================================================


class TestEdgeCases:
    """Edge cases and error handling for the LLM client."""

    def test_constructor_with_missing_fields_uses_defaults(self):
        """LLMClient should handle missing config fields with sensible defaults."""
        # Minimal config
        minimal = {"provider": "ollama", "model": "test"}
        try:
            client = LLMClient(config=minimal)
            # If it doesn't require all fields, check it has defaults
            assert client.provider == "ollama"
            assert client.model == "test"
        except (LLMClientError, KeyError):
            # Also acceptable to raise on missing required fields
            pass

    @pytest.mark.asyncio
    async def test_query_handles_http_error(self, client):
        """query must handle HTTP errors gracefully (return None or raise LLMClientError)."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.text = "Internal Server Error"
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            client._is_available = True
            try:
                result = await client.query("test")
                # Should return None or empty string on error
                assert result is None or result == ""
            except LLMClientError:
                pass  # Also acceptable

    @pytest.mark.asyncio
    async def test_query_handles_network_error(self, client):
        """query must handle network errors without crashing."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=ConnectionError("Network down"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            client._is_available = True
            try:
                result = await client.query("test")
                assert result is None or result == ""
            except LLMClientError:
                pass  # Also acceptable

    @pytest.mark.asyncio
    async def test_query_with_empty_prompt(self, client):
        """query with empty prompt should still work or return appropriate response."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"response": ""}
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            client._is_available = True
            result = await client.query("")
            assert isinstance(result, (str, type(None)))

    def test_constructor_requires_config(self):
        """LLMClient constructor must require a config parameter."""
        with pytest.raises(TypeError):
            LLMClient()  # Missing required config

    @pytest.mark.asyncio
    async def test_health_check_timeout(self, client):
        """health_check should handle timeout gracefully."""
        with patch("orchestrator.llm_client.httpx.AsyncClient") as MockHttpx:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=TimeoutError("Timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockHttpx.return_value = mock_client

            # Must not raise
            result = await client.health_check()
            assert result is False
