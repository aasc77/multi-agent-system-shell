"""
Tests for knowledge store — index_document, search across collections, and MCP tools.

Usage:
    cd /path/to/multi-agent-system-shell
    python3 -m pytest knowledge-store/tests/test_store.py -v
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

# Add knowledge-store to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_chromadb(tmp_path):
    """Use a temporary ChromaDB path for each test to avoid cross-test pollution."""
    store.CHROMADB_PATH = str(tmp_path / "chromadb")
    store._client = None
    store._collections.clear()
    yield
    store._client = None
    store._collections.clear()


# Fake embedding: deterministic 768-dim vector based on text hash
def _fake_embedding(text: str) -> list[float]:
    import hashlib
    h = hashlib.sha256(text.encode()).digest()
    vec = [b / 255.0 for b in h]
    # Pad to 768 dims
    return (vec * (768 // len(vec) + 1))[:768]


@pytest.fixture(autouse=True)
def _mock_ollama():
    """Mock Ollama embedding calls with deterministic fake embeddings."""
    async def mock_get_embedding(text):
        return _fake_embedding(text)

    with patch.object(store, "get_embedding", side_effect=mock_get_embedding):
        yield


# ---------------------------------------------------------------------------
# Tests: index_document
# ---------------------------------------------------------------------------

class TestIndexDocument:
    @pytest.mark.asyncio
    async def test_index_basic(self):
        doc_id = await store.index_document(
            text="NATS is the message bus for the MAS system.",
            title="NATS Overview",
            category="architecture",
        )
        assert doc_id == "ops-nats-overview"

        # Verify it's in the operational_knowledge collection
        coll = store._get_collection(store.COLLECTION_OPS_KNOWLEDGE)
        assert coll.count() == 1

        result = coll.get(ids=[doc_id], include=["documents", "metadatas"])
        assert "NATS" in result["documents"][0]
        assert result["metadatas"][0]["title"] == "NATS Overview"
        assert result["metadatas"][0]["category"] == "architecture"
        assert "updated_at" in result["metadatas"][0]

    @pytest.mark.asyncio
    async def test_index_custom_id(self):
        doc_id = await store.index_document(
            text="Custom doc",
            title="Custom",
            doc_id="my-custom-id",
        )
        assert doc_id == "my-custom-id"

    @pytest.mark.asyncio
    async def test_index_empty_text_raises(self):
        with pytest.raises(ValueError, match="empty"):
            await store.index_document(text="   ", title="Empty")

    @pytest.mark.asyncio
    async def test_index_idempotent_upsert(self):
        """Indexing the same title twice should upsert, not duplicate."""
        await store.index_document(text="Version 1", title="Test Doc")
        await store.index_document(text="Version 2", title="Test Doc")

        coll = store._get_collection(store.COLLECTION_OPS_KNOWLEDGE)
        assert coll.count() == 1

        result = coll.get(ids=["ops-test-doc"], include=["documents"])
        assert "Version 2" in result["documents"][0]

    @pytest.mark.asyncio
    async def test_index_does_not_pollute_messages_collection(self):
        await store.index_document(text="Ops doc", title="Ops")

        msg_coll = store._get_collection(store.COLLECTION_MESSAGES)
        assert msg_coll.count() == 0


# ---------------------------------------------------------------------------
# Tests: index_message (existing, verify no regression)
# ---------------------------------------------------------------------------

class TestIndexMessage:
    @pytest.mark.asyncio
    async def test_index_message_basic(self):
        msg = {
            "message_id": "msg-001",
            "from": "hub",
            "timestamp": "2026-04-03T10:00:00Z",
            "type": "agent_message",
            "priority": "normal",
            "message": "Task completed successfully.",
        }
        result = await store.index_message(msg, "agents.hub.outbox")
        assert result is True

        coll = store._get_collection(store.COLLECTION_MESSAGES)
        assert coll.count() == 1

    @pytest.mark.asyncio
    async def test_index_message_no_id_skips(self):
        result = await store.index_message({"message": "no id"}, "agents.hub.outbox")
        assert result is False


# ---------------------------------------------------------------------------
# Tests: search across collections
# ---------------------------------------------------------------------------

class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_from_both_collections(self):
        # Index a message
        await store.index_message(
            {
                "message_id": "msg-100",
                "from": "hub",
                "message": "Deployed the API gateway",
            },
            "agents.hub.outbox",
        )

        # Index an ops doc
        await store.index_document(
            text="The API gateway runs on port 8080",
            title="API Gateway Config",
            category="config",
        )

        results = await store.search("API gateway", n_results=10)
        assert len(results) == 2

        sources = {r["source"] for r in results}
        assert store.COLLECTION_MESSAGES in sources
        assert store.COLLECTION_OPS_KNOWLEDGE in sources

    @pytest.mark.asyncio
    async def test_search_single_collection(self):
        await store.index_document(
            text="Ops only doc",
            title="Ops Only",
            category="general",
        )

        results = await store.search(
            "Ops only",
            collections=[store.COLLECTION_OPS_KNOWLEDGE],
        )
        assert len(results) == 1
        assert results[0]["source"] == store.COLLECTION_OPS_KNOWLEDGE

    @pytest.mark.asyncio
    async def test_search_empty_collections(self):
        results = await store.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_results_sorted_by_distance(self):
        await store.index_document(text="Alpha bravo", title="Doc A")
        await store.index_document(text="Charlie delta", title="Doc B")

        results = await store.search("Alpha bravo", n_results=2)
        assert len(results) <= 2
        # Results should be sorted by distance ascending
        if len(results) == 2:
            assert results[0]["distance"] <= results[1]["distance"]

    @pytest.mark.asyncio
    async def test_search_respects_n_results(self):
        for i in range(5):
            await store.index_document(text=f"Document {i}", title=f"Doc {i}")

        results = await store.search("Document", n_results=3)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# Tests: MCP server tools
# ---------------------------------------------------------------------------

class TestMCPTools:
    @pytest.mark.asyncio
    async def test_search_knowledge_tool(self):
        import server

        await store.index_document(
            text="NATS runs on port 4222",
            title="NATS Config",
            category="config",
        )

        result = await server.call_tool("search_knowledge", {"query": "NATS port"})
        assert len(result) == 1
        text = result[0].text
        data = json.loads(text)
        assert len(data) >= 1
        assert "4222" in data[0]["text"]

    @pytest.mark.asyncio
    async def test_search_knowledge_source_filter(self):
        import server

        await store.index_document(text="Ops info", title="Ops", category="config")

        result = await server.call_tool("search_knowledge", {
            "query": "Ops info",
            "source": "messages",
        })
        # Should find nothing since we only indexed to ops
        assert "No matching" in result[0].text

    @pytest.mark.asyncio
    async def test_index_knowledge_tool(self):
        import server

        result = await server.call_tool("index_knowledge", {
            "title": "Test Knowledge",
            "content": "This is test operational knowledge.",
            "category": "general",
        })
        assert "Indexed" in result[0].text
        assert "Test Knowledge" in result[0].text

        # Verify it was actually stored
        coll = store._get_collection(store.COLLECTION_OPS_KNOWLEDGE)
        assert coll.count() == 1

    @pytest.mark.asyncio
    async def test_index_knowledge_missing_title(self):
        import server

        result = await server.call_tool("index_knowledge", {
            "content": "No title provided",
        })
        assert "Error" in result[0].text

    @pytest.mark.asyncio
    async def test_index_knowledge_missing_content(self):
        import server

        result = await server.call_tool("index_knowledge", {
            "title": "Has title",
        })
        assert "Error" in result[0].text

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        import server

        result = await server.call_tool("nonexistent_tool", {})
        assert "Unknown tool" in result[0].text
