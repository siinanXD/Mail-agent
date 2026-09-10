"""Objekt-Erkennung, Umbuchungen und Dublettenschutz im Dauerbetrieb."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.database import repositories as repo
from app.email.extractor import EmailExtraction
from app.email.importer import import_emails, import_directory
from app.email.parser import ParsedEmail
from app.units import normalize_unit_name
from tests.fakes import rule_based_extractor


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ferienwohnung Seeblick", "seeblick"),
        ("FeWo Seeblick", "seeblick"),
        ("fewo  seeblick!", "seeblick"),
        ("Wohnung Seeblick", "seeblick"),
        ("Haus Bergblick (OG)", "bergblick og"),
        ("Ferienwohnung Müllerhof", "muellerhof"),
        ("Apartment 3", "3"),
        ("Ferienwohnung", "ferienwohnung"),
        ("", ""),
    ],
)
def test_normalisierung_fasst_schreibvarianten_zusammen(raw, expected):
    assert normalize_unit_name(raw) == expected


def test_objekte_werden_nicht_doppelt_angelegt(session):
    first = repo.get_or_create_unit(session, "Ferienwohnung Seeblick")
    second = repo.get_or_create_unit(session, "FeWo Seeblick")
    third = repo.get_or_create_unit(session, "Haus Bergblick")

    assert first.id == second.id
    assert first.name == "Ferienwohnung Seeblick"  # erster Schreibweise treu
    assert third.id != first.id
    assert len(repo.list_units(session)) == 2


def test_leerer_objektname_erzeugt_kein_objekt(session):
    assert repo.get_or_create_unit(session, "   ") is None
    assert repo.list_units(session) == []


def test_buchungen_landen_am_richtigen_objekt(session, sample_dir):
    import_directory(
        session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: []
    )
    session.commit()

    seeblick = repo.search_bookings(session, unit_name="Seeblick")
    references = {b.booking_reference for b in seeblick}

    # BK-2026-0107 kam als "FeWo Seeblick" herein und muss trotzdem hier landen.
    assert {"BK-2026-0101", "BK-2026-0103", "BK-2026-0106", "BK-2026-0107"} <= references
    assert all(b.unit is not None for b in seeblick)


def test_umbuchung_verschiebt_die_buchung_und_wird_protokolliert(session, sample_dir):
    import_directory(
        session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: []
    )
    session.commit()

    booking = repo.get_booking_by_reference(session, "BK-2026-0108")
    assert booking.arrival_date == date(2026, 9, 10)  # war 09.09.
    assert booking.departure_date == date(2026, 9, 13)

    changes = repo.search_booking_changes(session, booking_reference="BK-2026-0108")
    assert len(changes) == 1
    assert changes[0].field == "arrival_date"
    assert changes[0].old_value == "2026-09-09"
    assert changes[0].new_value == "2026-09-10"
    assert changes[0].source_email_id is not None


def test_objektwechsel_wird_uebernommen(session):
    repo.upsert_booking(
        session,
        booking_reference="BK-2026-0900",
        guest_name="Tom Klein",
        arrival_date=date(2026, 10, 1),
        departure_date=date(2026, 10, 5),
        unit_id=repo.get_or_create_unit(session, "Ferienwohnung Seeblick").id,
    )
    session.commit()

    import_emails(
        session,
        [
            ParsedEmail(
                provider_message_id="mail-objektwechsel",
                subject="Änderung BK-2026-0900",
                body="Wir würden gerne ins Haus Anna wechseln.",
                received_at=datetime(2026, 9, 20, 10, 0),
            )
        ],
        extractor=lambda mail: EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0900",
            new_unit_name="Haus Anna",
        ),
        embedder=lambda c: [],
    )
    session.commit()

    booking = repo.get_booking_by_reference(session, "BK-2026-0900")
    assert booking.unit.name == "Haus Anna"
    change = repo.search_booking_changes(session, booking_reference="BK-2026-0900")[0]
    assert change.field == "unit"
    assert change.old_value == "Ferienwohnung Seeblick"
    assert change.new_value == "Haus Anna"


def test_bekannte_mails_werden_ohne_extraktion_uebersprungen(session, sample_dir):
    """Dublettenbremse fuer den Dauerbetrieb: kein zweiter LLM-Call."""
    from app.email.parser import load_directory

    emails = load_directory(sample_dir)
    first = import_emails(
        session, emails, extractor=rule_based_extractor, embedder=lambda c: []
    )
    session.commit()
    assert first.imported == 14
    assert first.skipped == 0

    calls: list[str] = []

    def zaehlender_extractor(mail):
        calls.append(mail.provider_message_id)
        return rule_based_extractor(mail)

    second = import_emails(
        session, emails, extractor=zaehlender_extractor, embedder=lambda c: []
    )

    assert second.imported == 0
    assert second.skipped == 14
    assert calls == []  # kein einziger Extraktions-Aufruf
