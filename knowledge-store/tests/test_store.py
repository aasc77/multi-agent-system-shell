"""Unit tests for knowledge-store/store.py."""

import json
from unittest.mock import AsyncMock, patch

import chromadb
import pytest
import pytest_asyncio

# Patch environment before importing store
import os
os.environ["CHROMADB_PATH"] = "/tmp/test-chromadb"
os.environ["OLLAMA_URL"] = "http://localhost:11434"

import store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_EMBEDDING = [0.1] * 768


@pytest.fixture(autouse=True)
def reset_store():
    """Reset store globals between tests."""
    store._client = None
    store._collection = None
    yield
    store._client = None
    store._collection = None


@pytest.fixture
def memory_collection():
    """In-memory ChromaDB collection for testing."""
    client = chromadb.EphemeralClient()
    # Delete collection if it exists from a prior test
    try:
        client.delete_collection("test_messages")
    except Exception:
        pass
    collection = client.create_collection(
        name="test_messages",
        metadata={"hnsw:space": "cosine"},
    )
    # Inject into store module
    store._client = client
    store._collection = collection
    return collection


# ---------------------------------------------------------------------------
# build_text tests
# ---------------------------------------------------------------------------

class TestBuildText:
    def test_extracts_message_field(self):
        msg = {"message": "hello world", "message_id": "x", "from": "hub"}
        assert store.build_text(msg) == "hello world"

    def test_extracts_summary_field(self):
        msg = {"summary": "task done", "status": "pass"}
        assert store.build_text(msg) == "task done"

    def test_combines_multiple_fields(self):
        msg = {"message": "hello", "summary": "world"}
        assert store.build_text(msg) == "hello world"

    def test_fallback_to_json(self):
        msg = {"status": "pass", "custom_data": "value"}
        result = store.build_text(msg)
        assert "custom_data" in result
        assert "value" in result

    def test_empty_message(self):
        # Only metadata fields — nothing indexable
        msg = {"message_id": "x", "timestamp": "t", "from": "hub", "priority": "normal", "type": "agent_complete"}
        assert store.build_text(msg) == ""

    def test_ignores_non_string_values(self):
        msg = {"message": 123, "summary": "ok"}
        assert store.build_text(msg) == "ok"


# ---------------------------------------------------------------------------
# index_message tests
# ---------------------------------------------------------------------------

class TestIndexMessage:
    @pytest.mark.asyncio
    @patch.object(store, "get_embedding", new_callable=AsyncMock, return_value=FAKE_EMBEDDING)
    async def test_indexes_message(self, mock_embed, memory_collection):
        msg = {
            "type": "agent_message",
            "message_id": "hub-1234-1",
            "timestamp": "2026-04-01T00:00:00Z",
            "from": "hub",
            "priority": "normal",
            "message": "deploy the service",
        }
        result = await store.index_message(msg, "agents.dgx.inbox")
        assert result is True

        # Verify it's in ChromaDB
        stored = memory_collection.get(ids=["hub-1234-1"], include=["documents", "metadatas"])
        assert stored["documents"][0] == "deploy the service"
        assert stored["metadatas"][0]["from"] == "hub"
        assert stored["metadatas"][0]["to"] == "dgx"
        assert stored["metadatas"][0]["channel"] == "inbox"

    @pytest.mark.asyncio
    async def test_skips_without_message_id(self, memory_collection):
        msg = {"message": "no id here"}
        result = await store.index_message(msg, "agents.hub.inbox")
        assert result is False

    @pytest.mark.asyncio
    @patch.object(store, "get_embedding", new_callable=AsyncMock, return_value=FAKE_EMBEDDING)
    async def test_idempotent_upsert(self, mock_embed, memory_collection):
        msg = {
            "message_id": "hub-1234-1",
            "from": "hub",
            "message": "first version",
        }
        await store.index_message(msg, "agents.hub.outbox")
        await store.index_message(msg, "agents.hub.outbox")

        # Should only have one document
        stored = memory_collection.get(ids=["hub-1234-1"])
        assert len(stored["ids"]) == 1

    @pytest.mark.asyncio
    @patch.object(store, "get_embedding", new_callable=AsyncMock, return_value=FAKE_EMBEDDING)
    async def test_outbox_metadata(self, mock_embed, memory_collection):
        msg = {
            "message_id": "dgx-5678-2",
            "from": "dgx",
            "summary": "inference complete",
            "type": "agent_complete",
        }
        await store.index_message(msg, "agents.dgx.outbox")
        stored = memory_collection.get(ids=["dgx-5678-2"], include=["metadatas"])
        assert stored["metadatas"][0]["from"] == "dgx"
        assert stored["metadatas"][0]["to"] == ""  # outbox has no target
        assert stored["metadatas"][0]["channel"] == "outbox"


# ---------------------------------------------------------------------------
# search tests
# ---------------------------------------------------------------------------

class TestSearch:
    @pytest.mark.asyncio
    @patch.object(store, "get_embedding", new_callable=AsyncMock, return_value=FAKE_EMBEDDING)
    async def test_search_returns_results(self, mock_embed, memory_collection):
        # Pre-populate
        memory_collection.upsert(
            ids=["msg-1", "msg-2"],
            embeddings=[FAKE_EMBEDDING, [0.2] * 768],
            documents=["deploy service to prod", "run unit tests"],
            metadatas=[
                {"from": "hub", "to": "dgx", "channel": "inbox", "subject": "agents.dgx.inbox", "timestamp": "", "type": "", "priority": "normal"},
                {"from": "macmini", "to": "", "channel": "outbox", "subject": "agents.macmini.outbox", "timestamp": "", "type": "", "priority": "normal"},
            ],
        )
        results = await store.search("deploy", n_results=5)
        assert len(results) == 2
        assert results[0]["id"] in ("msg-1", "msg-2")
        assert "text" in results[0]
        assert "metadata" in results[0]
        assert "distance" in results[0]

    @pytest.mark.asyncio
    @patch.object(store, "get_embedding", new_callable=AsyncMock, return_value=FAKE_EMBEDDING)
    async def test_search_with_filter(self, mock_embed, memory_collection):
        memory_collection.upsert(
            ids=["msg-1", "msg-2"],
            embeddings=[FAKE_EMBEDDING, FAKE_EMBEDDING],
            documents=["from hub", "from dgx"],
            metadatas=[
                {"from": "hub", "to": "", "channel": "outbox", "subject": "agents.hub.outbox", "timestamp": "", "type": "", "priority": "normal"},
                {"from": "dgx", "to": "", "channel": "outbox", "subject": "agents.dgx.outbox", "timestamp": "", "type": "", "priority": "normal"},
            ],
        )
        results = await store.search("test", n_results=5, where={"from": "hub"})
        assert len(results) == 1
        assert results[0]["metadata"]["from"] == "hub"

    @pytest.mark.asyncio
    @patch.object(store, "get_embedding", new_callable=AsyncMock, return_value=FAKE_EMBEDDING)
    async def test_search_empty_collection(self, mock_embed, memory_collection):
        results = await store.search("anything", n_results=5)
        assert results == []


# ---------------------------------------------------------------------------
# check_ollama_health tests
# ---------------------------------------------------------------------------

class TestCheckOllamaHealth:
    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    async def test_healthy(self, mock_get):
        mock_get.return_value = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"models": [{"name": "nomic-embed-text:latest"}]},
        })()
        ok, msg = await store.check_ollama_health()
        assert ok is True
        assert msg == "OK"

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    async def test_model_missing(self, mock_get):
        mock_get.return_value = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"models": [{"name": "qwen3:8b"}]},
        })()
        ok, msg = await store.check_ollama_health()
        assert ok is False
        assert "nomic-embed-text" in msg

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=Exception("connection refused"))
    async def test_unreachable(self, mock_get):
        ok, msg = await store.check_ollama_health()
        assert ok is False
        assert "unreachable" in msg.lower()
