"""Abrufzeiten des Watchers und der Alembic-Stand des Schemas."""

from __future__ import annotations

from datetime import datetime, time

import pytest
from sqlalchemy import inspect, text

from app.config import parse_poll_times
from app.email.watcher import next_run_at

SCHEDULE = [time(0, 0), time(12, 0), time(18, 0)]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00:00,12:00,18:00", [time(0, 0), time(12, 0), time(18, 0)]),
        (" 6:30 , 18:00 ", [time(6, 30), time(18, 0)]),
        ("12", [time(12, 0)]),
        ("18:00,18:00", [time(18, 0)]),  # dedupliziert
        ("12:00,quatsch,07:15", [time(7, 15), time(12, 0)]),  # sortiert, gefiltert
        ("", []),
    ],
)
def test_poll_times_werden_geparst(raw, expected):
    assert parse_poll_times(raw) == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # vor der ersten Zeit des Tages
        (datetime(2026, 9, 10, 8, 30), datetime(2026, 9, 10, 12, 0)),
        # zwischen zwei Zeiten
        (datetime(2026, 9, 10, 12, 1), datetime(2026, 9, 10, 18, 0)),
        # nach der letzten Zeit -> naechster Tag
        (datetime(2026, 9, 10, 18, 0), datetime(2026, 9, 11, 0, 0)),
        (datetime(2026, 9, 10, 23, 59), datetime(2026, 9, 11, 0, 0)),
        # Monatswechsel
        (datetime(2026, 9, 30, 19, 0), datetime(2026, 10, 1, 0, 0)),
    ],
)
def test_naechster_abruf(now, expected):
    assert next_run_at(now, SCHEDULE) == expected


def test_ohne_zeiten_wird_nicht_dauerhaft_gepollt():
    """Leerer Zeitplan darf keine Endlosschleife ohne Wartezeit ergeben."""
    naechster = next_run_at(datetime(2026, 9, 10, 8, 30), [])

    assert naechster == datetime(2026, 9, 11, 0, 0)


def test_migration_erzeugt_das_vollstaendige_schema(pg_session):
    """pg_session baut die DB ueber Alembic auf - hier die Gegenprobe."""
    inspector = inspect(pg_session.get_bind())
    tables = set(inspector.get_table_names())

    assert {
        "alembic_version",
        "tenants",
        "users",
        "mailboxes",
        "emails",
        "units",
        "bookings",
        "cancellations",
        "booking_changes",
        "email_embeddings",
    } <= tables

    spalten = {c["name"] for c in inspector.get_columns("bookings")}
    assert {"tenant_id", "unit_id", "booking_reference", "status"} <= spalten

    version = pg_session.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar()
    # Gegen den tatsaechlichen Head pruefen statt eine Revision fest einzutragen -
    # sonst bricht der Test bei jeder neuen Migration.
    from alembic.script import ScriptDirectory

    from app.database.connection import _alembic_config

    head = ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()
    assert version == head
