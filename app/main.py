"""FastAPI-Einstiegspunkt."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.admin import bootstrap
from app.api import (
    auth,
    calendar,
    chat,
    emails,
    health,
    mailbox,
    reports,
    staff,
    timeline,
    units,
)
from app.config import get_settings
from app.database.connection import init_db
from app.email import watcher
from app.observability.langfuse import flush, get_callback_handler
from app.staff import dispatcher

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        init_db()
        bootstrap()
    except Exception:
        logger.exception("Datenbank konnte nicht initialisiert werden.")
    get_callback_handler()

    tasks: list = []
    watcher.start(tasks)
    dispatcher.start(tasks)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        flush()


class UTF8JSONResponse(JSONResponse):
    """Deklariert charset=utf-8 explizit.

    JSON ist per Spec immer UTF-8, aber manche Clients (u.a. PowerShell 5.1
    Invoke-RestMethod) dekodieren ohne charset-Angabe als ISO-8859-1 und
    zerlegen dabei Umlaute.
    """

    media_type = "application/json; charset=utf-8"


app = FastAPI(
    title="Mail Agent",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(timeline.router)
app.include_router(chat.router)
app.include_router(chat.secure_router)
app.include_router(mailbox.router)
app.include_router(emails.router)
app.include_router(reports.router)
app.include_router(staff.router)
app.include_router(units.router)
app.include_router(calendar.router)

WEB_DIR = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """Die Weboberflaeche."""
    return FileResponse(WEB_DIR / "index.html")
