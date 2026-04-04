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
                "Search the shared knowledge base for past agent messages, conversations, "
                "and operational documentation. Uses semantic search — describe what you're "
                "looking for in natural language. Returns matching results from both agent "
                "messages and operational knowledge, merged by relevance."
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
                        "description": "Filter by source agent name (e.g., 'hub', 'dgx', 'macmini'). Only applies to agent_messages.",
                    },
                    "to_agent": {
                        "type": "string",
                        "description": "Filter by target agent name. Only applies to agent_messages.",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["all", "messages", "ops"],
                        "description": "Which collection to search: 'all' (default), 'messages' (agent messages only), 'ops' (operational knowledge only)",
                        "default": "all",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="index_knowledge",
            description=(
                "Store operational knowledge (system docs, runbooks, architecture notes) "
                "into the shared knowledge base. Use this to persist information that agents "
                "need to recall later — e.g., how NATS messaging works, IP addresses, "
                "startup procedures. Documents are deduplicated by title."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short title for the document (e.g., 'Tmux Pane Layout', 'Network Map')",
                    },
                    "content": {
                        "type": "string",
                        "description": "The document content to index",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["architecture", "runbook", "config", "status", "general"],
                        "description": "Category for the document (default: 'general')",
                        "default": "general",
                    },
                },
                "required": ["title", "content"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "search_knowledge":
        return await _handle_search(arguments)
    elif name == "index_knowledge":
        return await _handle_index(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_search(arguments: dict):
    query = arguments.get("query", "")
    if not query:
        return [TextContent(type="text", text="Error: query is required")]

    n_results = min(arguments.get("n_results", 5), 20)

    # Determine which collections to search
    source = arguments.get("source", "all")
    collections = None  # default: both
    if source == "messages":
        collections = [store.COLLECTION_MESSAGES]
    elif source == "ops":
        collections = [store.COLLECTION_OPS_KNOWLEDGE]

    # Build optional metadata filter (only meaningful for agent_messages)
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
        results = await store.search(
            query,
            n_results=n_results,
            where=where if source != "ops" else None,
            collections=collections,
        )
        if not results:
            return [TextContent(type="text", text="No matching results found.")]
        return [TextContent(type="text", text=json.dumps(results, indent=2))]
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"Knowledge search failed: {e}. Is Ollama running at {store.OLLAMA_URL}?",
        )]


async def _handle_index(arguments: dict):
    title = arguments.get("title", "")
    content = arguments.get("content", "")
    category = arguments.get("category", "general")

    if not title:
        return [TextContent(type="text", text="Error: title is required")]
    if not content:
        return [TextContent(type="text", text="Error: content is required")]

    try:
        doc_id = await store.index_document(
            text=content,
            title=title,
            category=category,
        )
        return [TextContent(
            type="text",
            text=f"Indexed operational knowledge: '{title}' (id={doc_id}, category={category})",
        )]
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"Failed to index knowledge: {e}. Is Ollama running at {store.OLLAMA_URL}?",
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
