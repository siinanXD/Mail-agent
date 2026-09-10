"""Semantische Suche ueber die E-Mail-Embeddings (pgvector)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.llm.client import embed_query

QueryEmbedder = Callable[[str], list[float]]


def semantic_search(
    session: Session,
    query: str,
    *,
    limit: int = 5,
    embedder: QueryEmbedder | None = None,
) -> list[dict[str, Any]]:
    """Liefert die aehnlichsten Chunks inkl. Metadaten und Score."""
    embed = embedder or embed_query
    vector = embed(query)
    hits = repo.search_embeddings(session, query_vector=vector, limit=limit)

    results: list[dict[str, Any]] = []
    for embedding, distance in hits:
        meta = embedding.meta or {}
        results.append(
            {
                "email_id": embedding.email_id,
                "subject": meta.get("subject"),
                "sender": meta.get("sender"),
                "received_at": meta.get("received_at"),
                "email_type": meta.get("email_type"),
                "chunk": embedding.chunk,
                "score": round(1.0 - distance, 4),
            }
        )
    return results
