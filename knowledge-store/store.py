"""
Knowledge Store — ChromaDB + Ollama embedding wrapper.

Provides indexing and semantic search over agent messages.
Storage: ChromaDB PersistentClient (local, no server).
Embeddings: Ollama nomic-embed-text (768-dim).
"""

import json
import logging
import os

import chromadb
import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------

CHROMADB_PATH = os.environ.get(
    "CHROMADB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "chromadb"),
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
COLLECTION_NAME = "agent_messages"

# ---------------------------------------------------------------------------
# ChromaDB client (lazy-initialized to avoid startup cost)
# ---------------------------------------------------------------------------

_client = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        os.makedirs(CHROMADB_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMADB_PATH)
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("ChromaDB collection '%s' ready at %s", COLLECTION_NAME, CHROMADB_PATH)
    return _collection


# ---------------------------------------------------------------------------
# Ollama embeddings
# ---------------------------------------------------------------------------

async def get_embedding(text: str) -> list[float]:
    """Get 768-dim embedding from Ollama nomic-embed-text."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": text},
        )
        response.raise_for_status()
        data = response.json()
        return data["embeddings"][0]


async def check_ollama_health() -> tuple[bool, str]:
    """Check if Ollama is reachable and the embedding model is available."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            # Check with and without :latest suffix
            if EMBED_MODEL not in models and f"{EMBED_MODEL}:latest" not in models:
                return False, f"Model '{EMBED_MODEL}' not found. Run: ollama pull {EMBED_MODEL}"
            return True, "OK"
    except Exception as e:
        return False, f"Ollama unreachable at {OLLAMA_URL}: {e}"


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def build_text(message: dict) -> str:
    """Extract searchable text from a NATS message envelope."""
    parts = []
    for key in ("message", "summary", "description", "title", "text"):
        val = message.get(key)
        if val and isinstance(val, str):
            parts.append(val)
    # Fallback: stringify content fields if no text fields found
    if not parts:
        content = {
            k: v for k, v in message.items()
            if k not in ("message_id", "timestamp", "from", "priority", "type")
        }
        if content:
            parts.append(json.dumps(content))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

async def index_message(message: dict, subject: str) -> bool:
    """
    Index a single NATS agent message into ChromaDB.

    Returns True if indexed, False if skipped.
    Uses message_id as document ID for idempotent upserts.
    """
    msg_id = message.get("message_id")
    if not msg_id:
        return False

    text = build_text(message)
    if not text:
        return False

    embedding = await get_embedding(text)

    # Parse subject: agents.<agent>.<channel>
    parts = subject.split(".")
    from_agent = message.get("from", parts[1] if len(parts) > 1 else "unknown")
    channel = parts[2] if len(parts) > 2 else "unknown"
    # For inbox messages, the subject agent is the recipient
    to_agent = parts[1] if channel == "inbox" else ""

    metadata = {
        "from": from_agent,
        "to": to_agent,
        "channel": channel,
        "subject": subject,
        "timestamp": message.get("timestamp", ""),
        "type": message.get("type", ""),
        "priority": message.get("priority", "normal"),
    }

    collection = _get_collection()
    collection.upsert(
        ids=[msg_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[metadata],
    )
    return True


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def search(query: str, n_results: int = 5, where: dict | None = None) -> list[dict]:
    """
    Semantic search over indexed messages.

    Returns list of dicts with id, text, metadata, and distance (lower = more similar).
    """
    embedding = await get_embedding(query)

    kwargs = {
        "query_embeddings": [embedding],
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where

    collection = _get_collection()
    results = collection.query(**kwargs)

    items = []
    for i in range(len(results["ids"][0])):
        items.append({
            "id": results["ids"][0][i],
            "text": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": round(results["distances"][0][i], 4),
        })
    return items
