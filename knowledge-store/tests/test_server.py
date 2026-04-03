"""Unit tests for knowledge-store/server.py."""

import json
from unittest.mock import AsyncMock, patch

import pytest

import store
import server


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------

class TestListTools:
    @pytest.mark.asyncio
    async def test_returns_search_knowledge_tool(self):
        tools = await server.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "search_knowledge"
        assert "query" in tools[0].inputSchema["properties"]
        assert "query" in tools[0].inputSchema["required"]


# ---------------------------------------------------------------------------
# call_tool
# ---------------------------------------------------------------------------

class TestCallTool:
    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        result = await server.call_tool("unknown_tool", {})
        assert result[0].text == "Unknown tool: unknown_tool"

    @pytest.mark.asyncio
    async def test_empty_query(self):
        result = await server.call_tool("search_knowledge", {"query": ""})
        assert "required" in result[0].text.lower()

    @pytest.mark.asyncio
    @patch.object(store, "search", new_callable=AsyncMock, return_value=[
        {"id": "hub-1-1", "text": "deploy the api", "metadata": {"from": "hub"}, "distance": 0.12},
    ])
    async def test_search_returns_results(self, mock_search):
        result = await server.call_tool("search_knowledge", {"query": "deploy api"})
        data = json.loads(result[0].text)
        assert len(data) == 1
        assert data[0]["id"] == "hub-1-1"
        mock_search.assert_called_once_with("deploy api", n_results=5, where=None)

    @pytest.mark.asyncio
    @patch.object(store, "search", new_callable=AsyncMock, return_value=[])
    async def test_search_no_results(self, mock_search):
        result = await server.call_tool("search_knowledge", {"query": "nonexistent"})
        assert "no matching" in result[0].text.lower()

    @pytest.mark.asyncio
    @patch.object(store, "search", new_callable=AsyncMock, return_value=[
        {"id": "msg-1", "text": "test", "metadata": {"from": "hub"}, "distance": 0.1},
    ])
    async def test_search_with_from_filter(self, mock_search):
        await server.call_tool("search_knowledge", {"query": "test", "from_agent": "hub"})
        mock_search.assert_called_once_with("test", n_results=5, where={"from": "hub"})

    @pytest.mark.asyncio
    @patch.object(store, "search", new_callable=AsyncMock, return_value=[
        {"id": "msg-1", "text": "test", "metadata": {"from": "hub", "to": "dgx"}, "distance": 0.1},
    ])
    async def test_search_with_both_filters(self, mock_search):
        await server.call_tool("search_knowledge", {
            "query": "test", "from_agent": "hub", "to_agent": "dgx",
        })
        mock_search.assert_called_once_with(
            "test", n_results=5,
            where={"$and": [{"from": "hub"}, {"to": "dgx"}]},
        )

    @pytest.mark.asyncio
    @patch.object(store, "search", new_callable=AsyncMock, return_value=[
        {"id": f"msg-{i}", "text": "x", "metadata": {}, "distance": 0.1}
        for i in range(20)
    ])
    async def test_n_results_capped_at_20(self, mock_search):
        await server.call_tool("search_knowledge", {"query": "test", "n_results": 100})
        mock_search.assert_called_once_with("test", n_results=20, where=None)

    @pytest.mark.asyncio
    @patch.object(store, "search", new_callable=AsyncMock, side_effect=Exception("Ollama down"))
    async def test_search_error_handling(self, mock_search):
        result = await server.call_tool("search_knowledge", {"query": "test"})
        assert "failed" in result[0].text.lower()
        assert "Ollama" in result[0].text
