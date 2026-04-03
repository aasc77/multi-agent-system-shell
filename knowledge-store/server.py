#!/usr/bin/env python3
"""
MCP stdio server for the Knowledge Store.

Exposes `search_knowledge` tool so agents can do semantic search
across shared agent message history via ChromaDB + Ollama embeddings.

Usage:
    AGENT_ROLE=hub OLLAMA_URL=http://localhost:11434 \
    CHROMADB_PATH=/path/to/data/chromadb python3 server.py
"""

import json
import os
import sys

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Add parent directory to path for store import
sys.path.insert(0, os.path.dirname(__file__))
import store

AGENT_ROLE = os.environ.get("AGENT_ROLE", "unknown")

server = Server("knowledge-store")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_knowledge",
            description=(
                "Search the shared knowledge base for past agent messages and conversations. "
                "Uses semantic search — describe what you're looking for in natural language. "
                "Returns matching messages with metadata (who sent it, when, relevance score). "
                "Optionally filter by source or target agent."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query (e.g., 'deployment instructions for the API')",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Max results to return (default: 5, max: 20)",
                        "default": 5,
                    },
                    "from_agent": {
                        "type": "string",
                        "description": "Filter by source agent name (e.g., 'hub', 'dgx', 'macmini')",
                    },
                    "to_agent": {
                        "type": "string",
                        "description": "Filter by target agent name",
                    },
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "search_knowledge":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    query = arguments.get("query", "")
    if not query:
        return [TextContent(type="text", text="Error: query is required")]

    n_results = min(arguments.get("n_results", 5), 20)

    # Build optional metadata filter
    where_clauses = []
    if arguments.get("from_agent"):
        where_clauses.append({"from": arguments["from_agent"]})
    if arguments.get("to_agent"):
        where_clauses.append({"to": arguments["to_agent"]})

    where = None
    if len(where_clauses) == 1:
        where = where_clauses[0]
    elif len(where_clauses) > 1:
        where = {"$and": where_clauses}

    try:
        results = await store.search(query, n_results=n_results, where=where)
        if not results:
            return [TextContent(type="text", text="No matching messages found.")]
        return [TextContent(type="text", text=json.dumps(results, indent=2))]
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"Knowledge search failed: {e}. Is Ollama running at {store.OLLAMA_URL}?",
        )]


async def main():
    # Health check on startup (log only, don't block)
    ok, msg = await store.check_ollama_health()
    if not ok:
        print(f"WARNING: {msg}", file=sys.stderr)
    else:
        print(f'Knowledge store MCP ready for "{AGENT_ROLE}"', file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    anyio.run(main)
