"""Tests der Agent-Tools gegen echte SQL- bzw. pgvector-Abfragen."""

from __future__ import annotations

import json

import pytest

from app.agent.tools import (
    check_occupancy,
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


def test_anzahl_ist_die_gesamtzahl_auch_wenn_die_liste_gekuerzt_ist(seeded):
    """Frueher war "count" die Laenge der auf limit gekuerzten Liste."""
    buchungen = call(search_bookings, limit=2)
    assert (buchungen["count"], buchungen["returned"], buchungen["truncated"]) == (8, 2, True)
    assert len(buchungen["bookings"]) == 2

    storno = call(search_cancellations, limit=1)
    assert (storno["count"], storno["returned"], storno["truncated"]) == (3, 1, True)

    bestaetigt = call(search_bookings, status="confirmed")
    assert (bestaetigt["count"], bestaetigt["truncated"]) == (5, False)


def test_mail_und_umbuchungssuche_melden_die_gesamtzahl(seeded):
    """Auch hier war "count" nur die Laenge der gekuerzten Liste."""
    from app.agent.tools.search_units import search_booking_changes

    mails = call(search_emails, limit=3)
    assert (mails["count"], mails["returned"], mails["truncated"]) == (14, 3, True)

    umbuchungen = call(search_booking_changes)
    assert (umbuchungen["count"], umbuchungen["returned"], umbuchungen["truncated"]) == (1, 1, False)


def test_das_llm_kann_keine_riesigen_listen_anfordern(seeded, monkeypatch):
    from app.agent.tools.common import MAX_RESULTS, clamp_limit
    from app.database import repositories

    angefordert: list[int] = []
    original = repositories.search_emails

    def aufzeichnen(session, *, limit, **filters):
        angefordert.append(limit)
        return original(session, limit=limit, **filters)

    monkeypatch.setattr(repositories, "search_emails", aufzeichnen)

    call(search_emails, limit=100_000)

    assert angefordert == [MAX_RESULTS]
    assert clamp_limit(0) == 1


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


def test_zur_buchungsnummer_gehoeren_auch_umbuchungsmails(seeded):
    """Umbuchungen haengen ueber booking_changes an der Buchung - die fehlten."""
    alle = call(search_emails, booking_reference="BK-2026-0108")
    assert "change" in {e["email_type"] for e in alle["emails"]}

    nur_umbuchung = call(search_emails, booking_reference="BK-2026-0108", email_type="change")
    assert nur_umbuchung["count"] == 1


# ---------------------------------------------------------------- Belegung


def test_belegung_findet_gaeste_die_vor_dem_zeitraum_angereist_sind(seeded):
    """search_bookings filtert nur die Anreise - "wer wohnt am 08.09.?" fand niemanden."""
    nur_anreise = call(
        search_bookings, start_date="2026-09-08", end_date="2026-09-08", unit_name="Bergblick"
    )
    assert nur_anreise["count"] == 0

    (bergblick,) = call(check_occupancy, start_date="2026-09-08", unit_name="Bergblick")["units"]

    assert any("Kowalski" in stay["guest"] for stay in bergblick["stays"])
    assert bergblick["free_nights"] == []
    assert bergblick["arrivals"] == []


def test_belegung_zeigt_den_wechseltag(seeded):
    """Meier reist am 12.09. aus der FeWo Seeblick ab, Yilmaz am selben Tag an."""
    (seeblick,) = call(check_occupancy, start_date="2026-09-12", unit_name="Seeblick")["units"]

    assert [stay["guest"] for stay in seeblick["departures"]] == ["Familie Meier"]
    assert [stay["guest"] for stay in seeblick["arrivals"]] == ["Emre Yilmaz"]
    assert seeblick["turnover_days"] == ["2026-09-12"]
    assert seeblick["cleaning_days"] == ["2026-09-12"]


def test_abreisetag_ist_frei_und_stornierte_buchung_zaehlt_nicht(seeded):
    (anna,) = call(check_occupancy, start_date="2026-09-13", unit_name="Haus Anna")["units"]
    assert anna["departures"]
    assert anna["free_nights"] == ["2026-09-13"]

    # Berger (FeWo Seeblick, 12.-15.09.) hat storniert.
    (seeblick,) = call(
        check_occupancy, start_date="2026-09-13", end_date="2026-09-14", unit_name="Seeblick"
    )["units"]
    assert all("Berger" not in stay["guest"] for stay in seeblick["stays"])


def test_belegung_fasst_reinigungen_anreisen_und_abreisen_zusammen(seeded):
    """Im Test mit echtem LLM fehlten Abreisen aus den Listen je Objekt, und freie
    Naechte wurden als Reinigungen gelesen - deshalb flache Uebersichten oben."""
    woche = call(check_occupancy, start_date="2026-09-07", end_date="2026-09-13")

    assert woche["cleanings_total"] == 3
    assert [(c["date"], c["unit"]) for c in woche["cleanings"]] == [
        ("2026-09-12", "FeWo Bergblick"),
        ("2026-09-12", "Ferienwohnung Seeblick"),
        ("2026-09-13", "Haus Anna"),
    ]

    samstag = call(check_occupancy, start_date="2026-09-12")
    abreisen = [entry["guest"] for entry in samstag["departures"]]
    assert len(abreisen) == 2
    assert "Familie Meier" in abreisen
    assert any("Kowalski" in guest for guest in abreisen)
    assert [entry["guest"] for entry in samstag["arrivals"]] == ["Emre Yilmaz"]


def test_reinigungen_wie_im_putzplan_aber_ohne_datei(seeded, monkeypatch):
    from app.reports import cleaning_plan

    def keine_datei(*args, **kwargs):
        raise AssertionError("check_occupancy darf keine Datei schreiben")

    monkeypatch.setattr(cleaning_plan, "write_workbook", keine_datei)

    result = call(check_occupancy, start_date="2026-09-07", end_date="2026-09-13")

    reinigungen = sum(len(unit["cleaning_days"]) for unit in result["units"])
    plan = cleaning_plan.build_plan(seeded, year=2026, week=37)
    assert reinigungen == plan.cleaning_count == 3
    assert {unit["unit"] for unit in result["units"]} == {row.unit_name for row in plan.rows}
    assert "download_path" not in result


@pytest.mark.parametrize(
    ("anfrage", "hinweis"),
    [
        ({"start_date": "2026-09-10", "end_date": "2026-09-01"}, "vor dem Beginn"),
        ({"start_date": "2026-01-01", "end_date": "2026-12-31"}, "62 Tage"),
        ({"start_date": "2026-09-10", "unit_name": "Schloss Gibtsnicht"}, "Kein Objekt"),
    ],
)
def test_ungueltige_belegungsanfragen_liefern_einen_hinweis(seeded, anfrage, hinweis):
    assert hinweis in call(check_occupancy, **anfrage)["error"]


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
