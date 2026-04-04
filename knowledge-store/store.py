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
COLLECTION_MESSAGES = "agent_messages"
COLLECTION_OPS_KNOWLEDGE = "operational_knowledge"

# ---------------------------------------------------------------------------
# ChromaDB client (lazy-initialized to avoid startup cost)
# ---------------------------------------------------------------------------

_client = None
_collections: dict[str, object] = {}


def _get_client():
    global _client
    if _client is None:
        os.makedirs(CHROMADB_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMADB_PATH)
        logger.info("ChromaDB client ready at %s", CHROMADB_PATH)
    return _client


def _get_collection(name: str = COLLECTION_MESSAGES):
    if name not in _collections:
        client = _get_client()
        _collections[name] = client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("ChromaDB collection '%s' ready", name)
    return _collections[name]


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

    collection = _get_collection(COLLECTION_MESSAGES)
    collection.upsert(
        ids=[msg_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[metadata],
    )
    return True


async def index_document(
    text: str,
    title: str,
    category: str = "general",
    doc_id: str | None = None,
) -> str:
    """
    Index an arbitrary document into the operational_knowledge collection.

    Args:
        text: The document content to index.
        title: Human-readable title for the document.
        category: Category tag (e.g. "architecture", "runbook", "config").
        doc_id: Optional explicit ID. Auto-generated from title if not provided.

    Returns:
        The document ID used for the upsert.
    """
    from datetime import datetime, timezone

    if not text.strip():
        raise ValueError("Document text cannot be empty")

    if doc_id is None:
        # Deterministic ID from title for idempotent re-indexing
        doc_id = "ops-" + title.lower().replace(" ", "-").replace("/", "-")[:80]

    embedding = await get_embedding(text)

    metadata = {
        "title": title,
        "category": category,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "collection_type": "operational_knowledge",
    }

    collection = _get_collection(COLLECTION_OPS_KNOWLEDGE)
    collection.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[metadata],
    )
    logger.info("Indexed ops document '%s' (category=%s)", title, category)
    return doc_id


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def search(
    query: str,
    n_results: int = 5,
    where: dict | None = None,
    collections: list[str] | None = None,
) -> list[dict]:
    """
    Semantic search over indexed data.

    Args:
        query: Natural language search query.
        n_results: Max results to return.
        where: Optional ChromaDB metadata filter.
        collections: Which collections to search. Defaults to both.

    Returns list of dicts with id, text, metadata, source, and distance.
    """
    if collections is None:
        collections = [COLLECTION_MESSAGES, COLLECTION_OPS_KNOWLEDGE]

    embedding = await get_embedding(query)

    all_items = []
    for coll_name in collections:
        try:
            collection = _get_collection(coll_name)
        except Exception:
            continue

        # Check if collection has any documents before querying
        count = collection.count()
        if count == 0:
            continue

        kwargs = {
            "query_embeddings": [embedding],
            "n_results": min(n_results, count),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            results = collection.query(**kwargs)
        except Exception as e:
            logger.warning("Search failed on collection '%s': %s", coll_name, e)
            continue

        for i in range(len(results["ids"][0])):
            all_items.append({
                "id": results["ids"][0][i],
                "text": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "source": coll_name,
                "distance": round(results["distances"][0][i], 4),
            })

    # Merge by relevance (lowest distance first) and trim
    all_items.sort(key=lambda x: x["distance"])
    return all_items[:n_results]
