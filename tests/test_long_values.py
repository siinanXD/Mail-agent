"""Zu lange Werte aus Mails duerfen den Import in Postgres nicht scheitern lassen.

SQLite ignoriert Spaltenlaengen, Postgres lehnt zu lange Werte ab. Eine Mail an
viele Empfaenger (lange To-Zeile) scheiterte deshalb bei jedem Versuch - und der
Watcher ging nach drei Versuchen endgueltig an ihr vorbei.
"""

from __future__ import annotations

from datetime import date, datetime

from app.database import repositories as repo


def test_lange_kopfzeilen_werden_gespeichert_und_wiedererkannt(pg_session):
    lange_id = "<" + "x" * 300 + "@example.com>"
    email = repo.upsert_email(
        pg_session,
        provider_message_id=lange_id,
        sender="Absender " + "S" * 300 + " <a@example.com>",
        recipient=", ".join(f"gast{index}@example.com" for index in range(30)),
        subject="Betreff " + "B" * 600,
        body="Text",
        received_at=datetime(2026, 9, 1, 9, 0),
        email_type="booking",
    )
    pg_session.flush()

    assert len(email.recipient) <= 255
    assert len(email.subject) <= 500
    # Die Dublettenpruefung muss die gekuerzte ID wiederfinden - sonst liefe
    # dieselbe Mail bei jedem Abruf erneut durch die bezahlte Extraktion.
    assert repo.known_message_ids(pg_session, [lange_id]) == {lange_id}

    # Zwei IDs mit gleichem Anfang bleiben zwei Mails.
    andere_id = lange_id[:-13] + "@anders.example>"
    repo.upsert_email(
        pg_session,
        provider_message_id=andere_id,
        sender="a",
        recipient="b",
        subject="c",
        body="d",
        received_at=datetime(2026, 9, 1, 9, 0),
        email_type="other",
    )
    pg_session.flush()
    assert repo.known_message_ids(pg_session, [lange_id, andere_id]) == {lange_id, andere_id}


def test_lange_extrahierte_werte_werden_gespeichert(pg_session):
    unit = repo.get_or_create_unit(pg_session, "Ferienhaus " + "N" * 300)
    lange_nummer = "BK-" + "7" * 100
    booking = repo.upsert_booking(
        pg_session,
        booking_reference=lange_nummer,
        guest_name="Gast " + "G" * 300,
        arrival_date=date(2026, 12, 1),
        departure_date=date(2026, 12, 5),
        unit_id=unit.id,
    )
    repo.add_booking_change(
        pg_session,
        booking_id=booking.id,
        changed_at=datetime(2026, 9, 2, 9, 0),
        field="unit",
        old_value="Alt " + "A" * 300,
        new_value="Neu " + "N" * 300,
        source_email_id=None,
    )
    pg_session.flush()

    assert repo.get_booking_by_reference(pg_session, lange_nummer).id == booking.id
    assert repo.get_or_create_unit(pg_session, "Ferienhaus " + "N" * 300).id == unit.id
