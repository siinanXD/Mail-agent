"""Engine, Session-Factory, Migrationen und die Datenbankrolle der Anwendung.

Zwei Rollen, zwei Aufgaben:

* **Owner** (``MIGRATION_DATABASE_URL``) - fuehrt Migrationen aus, legt die
  pgvector-Extension an, verwaltet die App-Rolle. Im Docker-Image ist das der
  Superuser.
* **App** (``DATABASE_URL``) - damit laufen alle Anfragen. Diese Rolle darf
  kein Superuser sein und kein BYPASSRLS haben: Superuser umgehen
  Row-Level-Security immer, auch mit ``FORCE ROW LEVEL SECURITY``. Liefe die
  App als Superuser, waere die Mandantentrennung in der Datenbank wirkungslos.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, make_url, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Sekunden, bis ein Verbindungsaufbau aufgibt. Ohne Grenze haengt eine falsch
#: konfigurierte Verbindung (z.B. "localhost" gegen einen reinen IPv4-Port)
#: minutenlang stumm, statt mit einer klaren Fehlermeldung zu scheitern.
CONNECT_TIMEOUT_SECONDS = 10


def connect_args_for(url: str) -> dict:
    """Verbindungs-Timeout fuer PostgreSQL; andere Treiber bleiben unberuehrt."""
    if make_url(url).get_backend_name() == "postgresql":
        return {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    return {}


_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args=connect_args_for(_settings.database_url),
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


@contextmanager
def session_scope() -> Iterator[Session]:
    """Session mit commit/rollback-Handling - ohne Mandant.

    Nur fuer die Verwaltungsebene (Mandanten, Nutzer, Postfaecher). Fuer
    Mandantendaten ``app.tenancy.tenant_session`` verwenden.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def migration_url() -> str:
    return _settings.migration_database_url or _settings.database_url


def _alembic_config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # "%" muss verdoppelt werden, sonst verschluckt es die ConfigParser-
    # Interpolation (relevant bei Sonderzeichen im Passwort).
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


#: Woran eine Datenbank ohne ``alembic_version`` erkennt, bis zu welcher Revision
#: ihr Schema reicht: (Revision, Tabelle, Spalte oder None fuer "Tabelle genuegt").
#: Neue Migrationen, die ein erkennbares Merkmal anlegen, hier ergaenzen.
SCHEMA_MARKERS: tuple[tuple[str, str, str | None], ...] = (
    ("0002", "tenants", None),
    ("0003", "mailboxes", "last_uid"),
    ("0004", "mailboxes", "retry_uid"),
    ("0005", "bookings", "state_as_of"),
    ("0006", "staff_members", None),
    ("0007", "units", "house_rules"),
    ("0008", "login_sessions", None),
    ("0009", "agent_runs", None),
)


def detect_revision(conn) -> str | None:
    """Revision eines unversionierten Schemas - None, wenn es keins gibt."""
    inspector = inspect(conn)
    if not inspector.has_table("emails"):
        return None
    revision = "0001"
    for candidate, table, column in SCHEMA_MARKERS:
        if not inspector.has_table(table):
            break
        if column and column not in {c["name"] for c in inspector.get_columns(table)}:
            break
        revision = candidate
    return revision


def ensure_schema(target_engine: Engine | None = None) -> None:
    """Bringt die Datenbank per Alembic auf den aktuellen Stand.

    Sonderfall Bestandsdatenbank: Wurden die Tabellen frueher noch mit
    ``create_all`` angelegt, fehlt die Tabelle ``alembic_version``. Dann wird
    der tatsaechlich vorhandene Stand gestempelt (``SCHEMA_MARKERS``) und alles
    Spaetere regulaer migriert. Blind auf head zu stempeln wuerde Mandanten,
    RLS und neuere Spalten ueberspringen - und spaetere Upgrades koennten das
    nicht mehr reparieren, weil die Version schon als aktuell gilt.
    """
    target_engine = target_engine or engine
    config = _alembic_config(target_engine.url.render_as_string(hide_password=False))

    with target_engine.connect() as conn:
        versioned = inspect(conn).has_table("alembic_version")
        existing = None if versioned else detect_revision(conn)

    if existing is not None:
        logger.info("Unversioniertes Schema auf Stand %s gefunden - wird gestempelt.", existing)
        command.stamp(config, existing)

    command.upgrade(config, "head")


def ensure_app_role(owner_engine: Engine, app_url: str) -> None:
    """Legt die App-Rolle an bzw. zieht sie nach - idempotent, bei jedem Start.

    Name und Passwort kommen aus ``app_url``. Die Rolle bekommt nur
    Datenrechte (kein DDL, kein Schreiben in ``alembic_version``) und
    ausdruecklich NOSUPERUSER / NOBYPASSRLS.
    """
    if owner_engine.dialect.name != "postgresql":
        return

    app = make_url(app_url)
    owner = owner_engine.url
    if not app.username or app.username == owner.username:
        logger.warning(
            "App und Migrationen nutzen dieselbe Datenbankrolle (%s) - "
            "App-Rolle wird nicht verwaltet.",
            owner.username,
        )
        return

    with owner_engine.begin() as conn:
        quote = conn.dialect.identifier_preparer.quote
        role = quote(app.username)
        database = quote(owner.database)
        password = (
            " PASSWORD '" + app.password.replace("'", "''") + "'" if app.password else ""
        )
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": app.username}
        ).scalar()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(text(f"{verb} ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS{password}"))

        conn.execute(text(f"GRANT CONNECT ON DATABASE {database} TO {role}"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        conn.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
        )
        conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))
        # Den Migrationsstand darf nur der Owner veraendern.
        conn.execute(text(f"REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {role}"))

    logger.info("App-Rolle %s ist eingerichtet (NOSUPERUSER, NOBYPASSRLS).", app.username)


def rls_status(target_engine: Engine | None = None) -> dict[str, object]:
    """Mit welcher Rolle verbindet die App - und greift RLS fuer sie?"""
    target_engine = target_engine or engine
    if target_engine.dialect.name != "postgresql":
        return {"role": None, "effective": False}
    with target_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
    return {"role": row[0], "effective": not (row[1] or row[2])}


def init_db() -> None:
    owner = create_engine(
        migration_url(),
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args_for(migration_url()),
    )
    try:
        ensure_schema(owner)
        ensure_app_role(owner, _settings.database_url)
    finally:
        owner.dispose()
    logger.info("Datenbankschema ist aktuell (Alembic head)")

    status = rls_status()
    if not status["effective"]:
        logger.warning(
            "ACHTUNG: Die App verbindet als '%s' - Superuser/BYPASSRLS. "
            "Row-Level-Security ist damit wirkungslos, die Mandanten trennt nur "
            "noch der Anwendungscode. DATABASE_URL auf eine eigene App-Rolle "
            "umstellen und MIGRATION_DATABASE_URL fuer den Owner setzen.",
            status["role"],
        )
