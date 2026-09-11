"""Alembic-Environment.

Die Datenbank-URL kommt immer aus der Anwendungskonfiguration, damit alembic.ini
kein Secret enthaelt: MIGRATION_DATABASE_URL (Owner), sonst DATABASE_URL. Die
App-Rolle aus DATABASE_URL darf weder DDL ausfuehren noch alembic_version
schreiben - ein manuelles ``alembic upgrade head`` braucht den Owner.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.database.models import Base

config = context.config

# Hat der Aufrufer bereits eine URL gesetzt (z.B. die Test-Datenbank), gilt die.
# Sonst kommt sie aus der Anwendungskonfiguration - bevorzugt die Owner-Verbindung.
if not config.get_main_option("sqlalchemy.url", None):
    settings = get_settings()
    url = settings.migration_database_url or settings.database_url
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
