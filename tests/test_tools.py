"""Tests der Agent-Tools gegen echte SQL- bzw. pgvector-Abfragen."""

from __future__ import annotations

import json

import pytest

from app.agent.tools import (
    count_cancellations,
    get_email,
    knowledge_search,
    search_bookings,
    search_cancellations,
    search_emails,
)
from app.email.importer import import_directory
from app.knowledge.retriever import semantic_search
from tests.fakes import fake_embedder, fake_query_embedder, rule_based_extractor

LAST_WEEK = {"start_date": "2026-08-31", "end_date": "2026-09-06"}


@pytest.fixture
def seeded(session, sample_dir, use_session):
    """Demo-Mails in SQLite importieren und die Tools darauf verdrahten."""
    import_directory(
        session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: []
    )
    session.commit()
    use_session(session)
    return session


def call(tool, **kwargs) -> dict:
    return json.loads(tool.invoke(kwargs))


def test_count_cancellations_liefert_exakte_zahl(seeded):
    assert call(count_cancellations, **LAST_WEEK)["count"] == 2
    assert call(count_cancellations)["count"] == 3
    assert (
        call(count_cancellations, start_date="2026-08-01", end_date="2026-08-31")[
            "count"
        ]
        == 1
    )


def test_search_bookings_nach_gast_und_zeitraum(seeded):
    result = call(search_bookings, guest_name="Berger")
    assert result["count"] == 1
    assert result["bookings"][0]["booking_reference"] == "BK-2026-0103"
    assert result["bookings"][0]["status"] == "cancelled"

    september = call(search_bookings, start_date="2026-09-01", end_date="2026-09-30")
    references = {b["booking_reference"] for b in september["bookings"]}
    assert references == {
        "BK-2026-0102",
        "BK-2026-0103",
        "BK-2026-0105",
        "BK-2026-0106",
        "BK-2026-0107",
        "BK-2026-0108",
    }

    confirmed = call(search_bookings, status="confirmed")
    assert confirmed["count"] == 5


def test_search_cancellations_mit_grund(seeded):
    result = call(search_cancellations, **LAST_WEEK)

    assert result["count"] == 2
    reasons = {c["reason"] for c in result["cancellations"]}
    assert "Flugausfall" in reasons
    assert all(c["source_email_id"] for c in result["cancellations"])


def test_search_emails_und_get_email(seeded):
    found = call(search_emails, booking_reference="BK-2026-0105")
    assert found["count"] == 1
    email_id = found["emails"][0]["email_id"]

    detail = call(get_email, email_id=email_id)
    assert "BK-2026-0105" in detail["body"]
    assert detail["email_type"] == "booking"

    komplett = call(get_email, email_id=999)
    assert "error" in komplett

    # Zur Buchungsnummer gehoert auch die Stornierungsmail:
    berger = call(search_emails, booking_reference="BK-2026-0103")
    typen = {e["email_type"] for e in berger["emails"]}
    assert typen == {"booking", "cancellation"}

    storno = call(
        search_emails, booking_reference="BK-2026-0103", email_type="cancellation"
    )
    assert storno["count"] == 1

    beschwerden = call(search_emails, email_type="complaint")
    assert beschwerden["count"] == 1
    assert "Beschwerde" in beschwerden["emails"][0]["subject"]


def test_semantische_suche_ueber_pgvector(pg_session, sample_dir, use_session):
    """Volle Kette: Import -> Chunks -> Embeddings -> pgvector-Suche."""
    result = import_directory(
        pg_session,
        sample_dir,
        extractor=rule_based_extractor,
        embedder=fake_embedder,
    )
    pg_session.commit()
    assert result.chunks >= 10

    hits = semantic_search(
        pg_session, "Flugausfall Stornierung", limit=3, embedder=fake_query_embedder
    )
    assert hits
    assert "Flugausfall" in hits[0]["chunk"]
    assert hits[0]["score"] > 0

    fruehstueck = semantic_search(
        pg_session, "Beschwerde Frühstück Buffet", limit=3, embedder=fake_query_embedder
    )
    assert "Frühstück" in fruehstueck[0]["chunk"]


def test_knowledge_search_tool(pg_session, sample_dir, use_session, monkeypatch):
    import_directory(
        pg_session, sample_dir, extractor=rule_based_extractor, embedder=fake_embedder
    )
    pg_session.commit()
    use_session(pg_session)
    monkeypatch.setattr("app.knowledge.retriever.embed_query", fake_query_embedder)

    result = call(knowledge_search, query="Late Check-out", limit=3)

    assert result["count"] > 0
    assert "Check-out" in result["results"][0]["chunk"]
    assert result["results"][0]["email_id"]
