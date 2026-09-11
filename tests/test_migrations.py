"""Migrationen: manuelle Alembic-Aufrufe und Bestandsdatenbanken."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.database.connection import _alembic_config, connect_args_for, ensure_schema
from tests.conftest import _test_database_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("altstand", ["0001", "0002"])
def test_unversionierte_altdatenbank_wird_nachmigriert(pg_engine, altstand):
    """Fehlt alembic_version, darf nicht blind auf head gestempelt werden.

    Sonst fehlen Mandanten, RLS und neuere Spalten dauerhaft - ein spaeteres
    Upgrade haelt die Datenbank ja fuer aktuell.
    """
    owner_url = _test_database_url()
    config = _alembic_config(owner_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    owner = create_engine(owner_url, connect_args=connect_args_for(owner_url))
    try:
        with owner.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        command.upgrade(config, altstand)
        with owner.begin() as conn:
            if altstand == "0001":
                conn.execute(
                    text(
                        "INSERT INTO emails (provider_message_id, sender, recipient, subject, "
                        "body, received_at, email_type, created_at) "
                        "VALUES ('alt-1', 'a', 'b', 's', 't', now(), 'other', now())"
                    )
                )
            # So sah eine Datenbank aus, die frueher per create_all entstand.
            conn.execute(text("DROP TABLE alembic_version"))

        ensure_schema(owner)

        with owner.connect() as conn:
            inspector = inspect(conn)
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
            assert inspector.has_table("tenants")
            assert "last_uid" in {c["name"] for c in inspector.get_columns("mailboxes")}
            if altstand == "0001":
                # Bestehende Mails landen im Standard-Mandanten.
                tenant = conn.execute(
                    text("SELECT tenant_id FROM emails WHERE provider_message_id = 'alt-1'")
                ).scalar()
                assert tenant == 1
    finally:
        owner.dispose()


def test_manuelles_alembic_nutzt_die_owner_verbindung(pg_engine):
    """Die App-Rolle darf kein DDL - ``alembic`` von Hand muss den Owner nehmen.

    DATABASE_URL zeigt hier absichtlich auf einen Port, auf dem niemand lauscht.
    Nimmt env.py die falsche URL, scheitert der Aufruf.
    """
    env = {
        **os.environ,
        "DATABASE_URL": (
            "postgresql+psycopg://niemand:falsch@127.0.0.1:1/gibtsnicht?connect_timeout=2"
        ),
        "MIGRATION_DATABASE_URL": _test_database_url(),
    }

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "(head)" in result.stdout
