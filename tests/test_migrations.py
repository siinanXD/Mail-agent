"""Migrationen: manuelle Alembic-Aufrufe und Bestandsdatenbanken."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import _test_database_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
