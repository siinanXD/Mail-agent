"""Fixtures: SQLite fuer die strukturierten Tabellen, Postgres fuer pgvector und RLS."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database.connection import ensure_app_role, ensure_schema
from app.database.models import STRUCTURED_TABLES, Base, Tenant
from app.tenancy import bind_tenant

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "data" / "sample_emails"

#: Mandant, in dem die Tests standardmaessig arbeiten.
TENANT_ID = 1

TOOL_MODULES = (
    "app.agent.tools.search_emails",
    "app.agent.tools.get_email",
    "app.agent.tools.search_bookings",
    "app.agent.tools.search_cancellations",
    "app.agent.tools.count_cancellations",
    "app.agent.tools.knowledge_search",
    "app.agent.tools.search_units",
    "app.agent.tools.create_cleaning_plan",
)


@pytest.fixture
def sample_dir() -> Path:
    return SAMPLE_DIR


@pytest.fixture
def sqlite_engine():
    """In-Memory-SQLite mit den Tabellen ohne pgvector-Spalte.

    StaticPool + check_same_thread=False, weil LangGraph Tools in Worker-Threads
    ausfuehrt und dabei dieselbe Verbindung braucht.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=STRUCTURED_TABLES)
    with sessionmaker(bind=engine)() as setup:
        setup.add_all(
            [
                Tenant(id=1, name="Standard", slug="standard", active=True),
                Tenant(id=2, name="Zweiter Mandant", slug="zweiter", active=True),
            ]
        )
        setup.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def session(sqlite_engine) -> Iterator[Session]:
    """Session im Mandanten 1."""
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    db = bind_tenant(factory(), TENANT_ID)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def other_session(sqlite_engine) -> Iterator[Session]:
    """Session im Mandanten 2 - auf derselben Datenbank."""
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    db = bind_tenant(factory(), 2)
    try:
        yield db
    finally:
        db.close()


#: Eigene App-Rolle fuer die Tests - Rollen gelten clusterweit, und die Tests
#: sollen das Passwort der echten App-Rolle nicht ueberschreiben.
TEST_APP_ROLE = "mailagent_test_app"
TEST_APP_PASSWORD = "nur-fuer-tests"


def _test_database_url() -> str:
    """Owner-Verbindung zur Test-Datenbank - niemals die Anwendungsdatenbank.

    Standard ist der Datenbankname aus MIGRATION_DATABASE_URL (sonst
    DATABASE_URL) mit Suffix ``_test``; TEST_DATABASE_URL ueberschreibt das.
    """
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    settings = get_settings()
    url = make_url(settings.migration_database_url or settings.database_url)
    return url.set(database=f"{url.database}_test").render_as_string(
        hide_password=False
    )


def _create_database_if_missing(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin.dispose()


@pytest.fixture
def pg_engine():
    """Postgres-Testdatenbank - verbunden als App-Rolle, damit RLS wirklich greift.

    Das Schema baut der Owner frisch ueber die echten Migrationen auf (ein
    Fehler in einer Migration fliegt so im Test auf). Die Tests selbst laufen
    dann als Nicht-Superuser: als Superuser waere RLS wirkungslos und jeder
    Mandanten-Test gruen, egal ob die Trennung funktioniert.
    """
    owner_url = _test_database_url()
    if make_url(owner_url).database == make_url(get_settings().database_url).database:
        pytest.skip(
            "TEST_DATABASE_URL zeigt auf die Anwendungsdatenbank - die Tests "
            "wuerden deren Daten loeschen."
        )

    app_url = (
        make_url(owner_url)
        .set(username=TEST_APP_ROLE, password=TEST_APP_PASSWORD)
        .render_as_string(hide_password=False)
    )
    try:
        _create_database_if_missing(owner_url)
        owner = create_engine(owner_url)
        with owner.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        ensure_schema(owner)
        ensure_app_role(owner, app_url)
        with owner.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (2, 'Zweiter Mandant', 'zweiter')")
            )
    except Exception as error:
        pytest.skip(f"Keine Postgres-Verbindung ({error.__class__.__name__}: {error})")

    app_engine = create_engine(app_url)
    yield app_engine
    app_engine.dispose()
    owner.dispose()


@pytest.fixture
def pg_session(pg_engine) -> Iterator[Session]:
    """Postgres-Session im Mandanten 1 - RLS ist aktiv."""
    db = bind_tenant(sessionmaker(bind=pg_engine, expire_on_commit=False)(), TENANT_ID)
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def use_session(monkeypatch):
    """Verdrahtet die Tools mit einer Test-Session statt der globalen Engine."""

    def _apply(db: Session) -> None:
        @contextmanager
        def scope(tenant_id: int | None = None) -> Iterator[Session]:
            yield db
            db.commit()

        for module in TOOL_MODULES:
            monkeypatch.setattr(importlib.import_module(module), "tenant_session", scope)

    return _apply
