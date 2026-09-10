"""Erzeugt Embeddings fuer eine E-Mail und legt sie in pgvector ab."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.database.models import Email
from app.knowledge.chunker import chunk_email
from app.llm.client import embed_texts

logger = logging.getLogger(__name__)

Embedder = Callable[[list[str]], list[list[float]]]


def index_email(session: Session, email: Email, embedder: Embedder | None = None) -> int:
    """Chunkt die E-Mail, erzeugt Embeddings und ersetzt alte Eintraege."""
    embed = embedder or embed_texts
    chunks = chunk_email(email.subject, email.body)
    if not chunks:
        return 0

    vectors = embed(chunks)
    if not vectors:
        return 0

    count = repo.replace_embeddings(
        session,
        email_id=email.id,
        chunks=chunks,
        vectors=vectors,
        metadata={
            "subject": email.subject,
            "sender": email.sender,
            "email_type": email.email_type,
            "received_at": email.received_at.isoformat(),
        },
    )
    logger.debug("E-Mail %s indexiert (%d Chunks)", email.id, count)
    return count
