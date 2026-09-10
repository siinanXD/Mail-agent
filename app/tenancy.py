"""Mandantenkontext.

Der aktive Mandant haengt an zwei Stellen:

* an der Session (``session.info["tenant_id"]``) - dort liest ihn der Datenzugriff
  beim Anlegen neuer Zeilen, und dort greift das RLS-Event unten.
* an einer ContextVar - damit Code, der seine eigene Session oeffnet (die
  Agent-Tools zum Beispiel), weiss, fuer wen er arbeitet.

Die eigentliche Absicherung macht PostgreSQL: Bei jedem Transaktionsbeginn wird
``app.tenant_id`` gesetzt, und die Row-Level-Security-Regeln lassen nur Zeilen
dieses Mandanten durch. Vergisst eine Abfrage den Filter, kommen trotzdem keine
fremden Daten zurueck.

Das Event ist wichtig: ``set_config(..., true)`` gilt nur fuer eine Transaktion.
Committet Code zwischendurch, beginnt eine neue - ohne das Event waere der
Mandant dann weg und jede Abfrage liefe ins Leere.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal

logger = logging.getLogger(__name__)

_current_tenant: ContextVar[int | None] = ContextVar("current_tenant_id", default=None)


class NoTenantError(RuntimeError):
    """Es wurde auf Mandantendaten zugegriffen, ohne einen Mandanten zu setzen."""


def get_current_tenant() -> int | None:
    return _current_tenant.get()


def require_current_tenant() -> int:
    tenant_id = _current_tenant.get()
    if tenant_id is None:
        raise NoTenantError(
            "Kein Mandant im Kontext - Zugriff auf Mandantendaten ist so nicht moeglich."
        )
    return tenant_id


@contextmanager
def use_tenant(tenant_id: int) -> Iterator[int]:
    """Setzt den Mandanten fuer die Dauer des Blocks."""
    token = _current_tenant.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _current_tenant.reset(token)


def bind_tenant(session: Session, tenant_id: int) -> Session:
    """Haengt den Mandanten an eine Session. Greift ab der naechsten Transaktion."""
    session.info["tenant_id"] = tenant_id
    return session


def tenant_id_for(session: Session) -> int:
    """Mandant fuer neue Zeilen: erst an der Session, sonst aus dem Kontext."""
    bound = session.info.get("tenant_id")
    return bound if bound is not None else require_current_tenant()


@event.listens_for(Session, "after_begin")
def _apply_rls(session: Session, transaction, connection) -> None:
    tenant_id = session.info.get("tenant_id")
    if tenant_id is None or connection.dialect.name != "postgresql":
        return
    connection.execute(
        text("SELECT set_config('app.tenant_id', :value, true)"),
        {"value": str(tenant_id)},
    )


@contextmanager
def tenant_session(tenant_id: int | None = None) -> Iterator[Session]:
    """Session, die nur die Daten eines Mandanten sieht.

    Ohne ``tenant_id`` wird der Mandant aus dem Kontext genommen.
    """
    resolved = tenant_id if tenant_id is not None else require_current_tenant()
    session = bind_tenant(SessionLocal(), resolved)
    try:
        with use_tenant(resolved):
            yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
