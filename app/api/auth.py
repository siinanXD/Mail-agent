"""Anmeldung mit Nutzerkonten. Jeder Nutzer gehoert zu genau einem Mandanten.

Sessions: zufaelliges Token im HttpOnly-Cookie, die Zuordnung Token -> Nutzer
liegt im Prozessspeicher (nach einem Neustart muss man sich neu anmelden). Bei
jeder Anfrage wird zusaetzlich geprueft, ob Nutzer und Mandant noch aktiv sind -
eine Deaktivierung wirkt sofort, nicht erst nach Ablauf der Sitzung.

Endpunkte mit Mandantendaten haengen an ``require_user`` und bekommen den
Mandanten daraus - nie aus einem Parameter, den der Client setzen koennte.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from threading import Lock

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import get_settings
from app.crypto import hash_password, verify_password
from app.database import accounts
from app.database.connection import session_scope
from app.database.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["auth"])

COOKIE_NAME = "mailagent_session"


@dataclass(frozen=True)
class CurrentUser:
    user_id: int
    tenant_id: int
    email: str
    tenant_name: str


#: Token -> (Nutzer, Ablaufzeitpunkt)
_sessions: dict[str, tuple[CurrentUser, datetime]] = {}

#: Bremse gegen Passwort-Raten: So viele Fehlversuche je Adresse und Absender-IP
#: innerhalb des Fensters, dann pausiert die Anmeldung. Je IP, damit ein
#: Angreifer nicht den echten Nutzer aussperren kann.
MAX_FAILED_LOGINS = 5
FAILED_LOGIN_WINDOW = timedelta(minutes=15)
#: Schutz gegen unbegrenztes Wachsen durch Anfragen mit immer neuen Adressen.
_MAX_TRACKED_LOGINS = 10_000
_failed_logins: dict[tuple[str, str], list[datetime]] = {}
_failed_logins_lock = Lock()


def _recent_failures(key: tuple[str, str], now: datetime) -> int:
    with _failed_logins_lock:
        recent = [t for t in _failed_logins.get(key, []) if now - t < FAILED_LOGIN_WINDOW]
        if recent:
            _failed_logins[key] = recent
        else:
            _failed_logins.pop(key, None)
        return len(recent)


def _record_failure(key: tuple[str, str], now: datetime) -> None:
    with _failed_logins_lock:
        if len(_failed_logins) >= _MAX_TRACKED_LOGINS:
            for stale in [k for k, times in _failed_logins.items() if now - times[-1] >= FAILED_LOGIN_WINDOW]:
                del _failed_logins[stale]
        _failed_logins.setdefault(key, []).append(now)


@lru_cache
def _dummy_hash() -> str:
    """Wird geprueft, wenn es die E-Mail gar nicht gibt.

    Sonst waere eine Anmeldung mit unbekannter Adresse messbar schneller als
    eine mit falschem Passwort - und man koennte gueltige Adressen erraten.
    """
    return hash_password(secrets.token_urlsafe(16))


def _lookup(token: str | None) -> CurrentUser | None:
    if not token:
        return None
    entry = _sessions.get(token)
    if entry is None:
        return None
    user, expires = entry
    if expires < datetime.now():
        _sessions.pop(token, None)
        return None
    if not _still_active(user):
        _sessions.pop(token, None)
        logger.info("Sitzung von %s beendet: Konto oder Mandant deaktiviert", user.email)
        return None
    return user


def _still_active(user: CurrentUser) -> bool:
    """Ist das Konto seit der Anmeldung deaktiviert oder verschoben worden?"""
    with session_scope() as session:
        account = session.get(User, user.user_id)
        return bool(
            account is not None
            and account.active
            and account.tenant_id == user.tenant_id
            and account.tenant.active
        )


def require_user(mailagent_session: str | None = Cookie(default=None)) -> CurrentUser:
    """Dependency fuer alle Endpunkte mit Mandantendaten."""
    user = _lookup(mailagent_session)
    if user is None:
        raise HTTPException(status_code=401, detail="Nicht angemeldet")
    return user


class LoginRequest(BaseModel):
    email: str
    password: str


class SessionInfo(BaseModel):
    authenticated: bool
    email: str | None = None
    tenant_name: str | None = None


@router.get("/session", response_model=SessionInfo)
def session_info(mailagent_session: str | None = Cookie(default=None)) -> SessionInfo:
    user = _lookup(mailagent_session)
    if user is None:
        return SessionInfo(authenticated=False)
    return SessionInfo(authenticated=True, email=user.email, tenant_name=user.tenant_name)


@router.post("/login", response_model=SessionInfo)
def login(request: LoginRequest, response: Response, http_request: Request) -> SessionInfo:
    settings = get_settings()
    now = datetime.now()
    attempt = (
        request.email.strip().lower(),
        http_request.client.host if http_request.client else "",
    )
    if _recent_failures(attempt, now) >= MAX_FAILED_LOGINS:
        logger.warning("Anmeldung fuer %s pausiert: zu viele Fehlversuche", attempt[0])
        raise HTTPException(
            status_code=429,
            detail="Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen.",
        )

    with session_scope() as session:
        user = accounts.get_user_by_email(session, request.email)
        stored = user.password_hash if user else _dummy_hash()
        password_ok = verify_password(request.password, stored)
        allowed = (
            password_ok
            and user is not None
            and user.active
            and user.tenant.active
        )
        current = (
            CurrentUser(
                user_id=user.id,
                tenant_id=user.tenant_id,
                email=user.email,
                tenant_name=user.tenant.name,
            )
            if allowed
            else None
        )

    if current is None:
        _record_failure(attempt, now)
        logger.warning("Fehlgeschlagene Anmeldung fuer %s", request.email.strip().lower())
        # Bewusst dieselbe Meldung fuer "gibt es nicht" und "falsches Passwort".
        raise HTTPException(status_code=401, detail="E-Mail oder Passwort stimmt nicht")

    with _failed_logins_lock:
        _failed_logins.pop(attempt, None)
    token = secrets.token_urlsafe(32)
    _sessions[token] = (current, datetime.now() + timedelta(hours=settings.session_hours))
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_hours * 3600,
        secure=settings.cookie_secure,
    )
    logger.info("Anmeldung %s (Mandant %s)", current.email, current.tenant_id)
    return SessionInfo(authenticated=True, email=current.email, tenant_name=current.tenant_name)


@router.post("/logout", status_code=204)
def logout(
    response: Response, mailagent_session: str | None = Cookie(default=None)
) -> None:
    _sessions.pop(mailagent_session or "", None)
    response.delete_cookie(COOKIE_NAME)
