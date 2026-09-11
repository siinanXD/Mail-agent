"""Health-Endpunkt.

Bewusst ohne Anmeldung und ohne mandantenbezogene Angaben - kein Hinweis darauf,
wie viele Mandanten oder Postfaecher es gibt.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.config import get_settings
from app.database.connection import engine, rls_status
from app.email import watcher
from app.messaging.whatsapp import whatsapp_configured
from app.observability.langfuse import get_callback_handler
from app.staff import dispatcher

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    database: str
    #: False, wenn die App als Superuser/BYPASSRLS verbindet - dann trennt die
    #: Datenbank die Mandanten nicht, nur noch der Code.
    rls_effective: bool
    llm_configured: bool
    langfuse_enabled: bool
    encryption_configured: bool
    watcher_enabled: bool
    watcher_running: bool
    watcher_last_run: str | None = None
    watcher_next_run: str | None = None
    whatsapp_configured: bool
    cleaning_dispatch_running: bool


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    rls_effective = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = "ok"
        rls_effective = bool(rls_status()["effective"])
    except Exception as error:
        database = f"error: {error.__class__.__name__}"

    settings = get_settings()
    healthy = database == "ok" and rls_effective
    return HealthResponse(
        status="ok" if healthy else "degraded",
        database=database,
        rls_effective=rls_effective,
        llm_configured=bool(settings.openai_api_key),
        langfuse_enabled=get_callback_handler() is not None,
        encryption_configured=bool(settings.encryption_key),
        watcher_enabled=settings.watch_enabled,
        watcher_running=watcher.state.running,
        watcher_last_run=watcher.state.last_run.isoformat() if watcher.state.last_run else None,
        watcher_next_run=watcher.state.next_run.isoformat() if watcher.state.next_run else None,
        whatsapp_configured=whatsapp_configured(settings),
        cleaning_dispatch_running=dispatcher.state.running,
    )
