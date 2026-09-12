"""Anmeldung, Registrierung und Passwort-Reset.

Jeder Nutzer gehoert zu genau einem Mandanten. Wer sich selbst registriert,
bekommt einen eigenen Mandanten und ist dessen erster Nutzer.

Sitzungen: zufaelliges Token im HttpOnly-Cookie, die Zuordnung steht in der
Datenbank (``login_sessions``) - ein Neustart meldet niemanden mehr ab. Bei
jeder Anfrage wird zusaetzlich geprueft, ob Nutzer und Mandant noch aktiv sind:
eine Deaktivierung wirkt sofort, nicht erst nach Ablauf der Sitzung.

Gegen das Erraten fremder Konten antworten Registrierung und Reset immer
gleich - ob es die Adresse gibt, erfaehrt nur ihr Postfach.

Endpunkte mit Mandantendaten haengen an ``require_user`` und bekommen den
Mandanten daraus - nie aus einem Parameter, den der Client setzen koennte.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.throttle import Throttle
from app.config import get_settings
from app.crypto import hash_password, verify_password
from app.database import accounts
from app.database.connection import session_scope
from app.database.models import CODE_PASSWORD_RESET, CODE_SIGNUP, User
from app.notify import (
    MailSendError,
    send_already_registered,
    send_reset_code,
    send_signup_code,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["auth"])

COOKIE_NAME = "mailagent_session"

MIN_PASSWORD_LENGTH = 10
#: Bewusst grosszuegig - eine strenge Adressgrammatik lehnt mehr gueltige
#: Adressen ab, als sie ungueltige faengt. Ob die Adresse wirklich existiert,
#: entscheidet ohnehin erst der Code aus der Mail.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

#: Passwort-Raten: so viele Fehlversuche je Adresse und Absender-IP, dann Pause.
login_throttle = Throttle(limit=5, window=timedelta(minutes=15))
#: Codeversand (Registrierung, erneut senden, Reset): begrenzt, damit niemand
#: ueber unsere Endpunkte fremde Postfaecher zumuellt.
code_throttle = Throttle(limit=3, window=timedelta(minutes=15))


@dataclass(frozen=True)
class CurrentUser:
    user_id: int
    tenant_id: int
    email: str
    tenant_name: str


@lru_cache
def _dummy_hash() -> str:
    """Wird geprueft, wenn es die E-Mail gar nicht gibt.

    Sonst waere eine Anmeldung mit unbekannter Adresse messbar schneller als
    eine mit falschem Passwort - und man koennte gueltige Adressen erraten.
    """
    return hash_password(secrets.token_urlsafe(16))


def _current(user: User) -> CurrentUser:
    return CurrentUser(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        tenant_name=user.tenant.name,
    )


def _usable(user: User | None) -> bool:
    """Darf sich dieser Nutzer anmelden bzw. angemeldet bleiben?"""
    return bool(
        user is not None
        and user.active
        and user.verified_at is not None
        and user.tenant.active
    )


def _lookup(token: str | None) -> CurrentUser | None:
    if not token:
        return None
    with session_scope() as session:
        user = accounts.get_session_user(session, token)
        if user is None:
            return None
        if not _usable(user):
            accounts.end_login_session(session, token)
            logger.info(
                "Sitzung von %s beendet: Konto oder Mandant deaktiviert", user.email
            )
            return None
        return _current(user)


def require_user(mailagent_session: str | None = Cookie(default=None)) -> CurrentUser:
    """Dependency fuer alle Endpunkte mit Mandantendaten."""
    user = _lookup(mailagent_session)
    if user is None:
        raise HTTPException(status_code=401, detail="Nicht angemeldet")
    return user


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _normalized(email: str) -> str:
    return email.strip().lower()


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Das Passwort braucht mindestens {MIN_PASSWORD_LENGTH} Zeichen.",
        )


def _start_session(user_id: int, response: Response) -> None:
    settings = get_settings()
    with session_scope() as session:
        token = accounts.create_login_session(
            session, user_id=user_id, hours=settings.session_hours
        )
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_hours * 3600,
        secure=settings.cookie_secure,
    )


# ---------------------------------------------------------------- Sitzung


class SessionInfo(BaseModel):
    authenticated: bool
    email: str | None = None
    tenant_name: str | None = None


@router.get("/session", response_model=SessionInfo)
def session_info(mailagent_session: str | None = Cookie(default=None)) -> SessionInfo:
    user = _lookup(mailagent_session)
    if user is None:
        return SessionInfo(authenticated=False)
    return SessionInfo(
        authenticated=True, email=user.email, tenant_name=user.tenant_name
    )


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login", response_model=SessionInfo)
def login(
    request: LoginRequest, response: Response, http_request: Request
) -> SessionInfo:
    now = datetime.now()
    email = _normalized(request.email)
    attempt = (email, _client_ip(http_request))
    if login_throttle.blocked(*attempt, now=now):
        logger.warning("Anmeldung fuer %s pausiert: zu viele Fehlversuche", email)
        raise HTTPException(
            status_code=429,
            detail="Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen.",
        )

    with session_scope() as session:
        user = accounts.get_user_by_email(session, email)
        stored = user.password_hash if user else _dummy_hash()
        password_ok = verify_password(request.password, stored)
        # Nur wer das Passwort kennt, erfaehrt, dass die Adresse noch unbestaetigt
        # ist - sonst liesse sich damit nach Konten suchen.
        unverified = bool(
            password_ok
            and user is not None
            and user.active
            and user.verified_at is None
        )
        current = _current(user) if password_ok and _usable(user) else None

    if unverified:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "unverified",
                "message": (
                    "Bitte zuerst die E-Mail bestaetigen. "
                    "Den Code findest du in deinem Postfach."
                ),
            },
        )
    if current is None:
        login_throttle.record(*attempt, now=now)
        logger.warning("Fehlgeschlagene Anmeldung fuer %s", email)
        # Bewusst dieselbe Meldung fuer "gibt es nicht" und "falsches Passwort".
        raise HTTPException(status_code=401, detail="E-Mail oder Passwort stimmt nicht")

    login_throttle.clear(*attempt)
    _start_session(current.user_id, response)
    logger.info("Anmeldung %s (Mandant %s)", current.email, current.tenant_id)
    return SessionInfo(
        authenticated=True, email=current.email, tenant_name=current.tenant_name
    )


@router.post("/logout", status_code=204)
def logout(
    response: Response, mailagent_session: str | None = Cookie(default=None)
) -> None:
    if mailagent_session:
        with session_scope() as session:
            accounts.end_login_session(session, mailagent_session)
    response.delete_cookie(COOKIE_NAME)


# ---------------------------------------------------------------- Registrierung


class RegisterRequest(BaseModel):
    email: str
    password: str
    #: Name der Vermietung - wird der Name des neuen Mandanten.
    company: str | None = None


class CodeSent(BaseModel):
    """Immer dieselbe Antwort, egal ob es die Adresse schon gab."""

    status: str = "code_sent"
    message: str = (
        "Wenn die Adresse verwendet werden kann, ist ein Code unterwegs. "
        "Bitte sieh in deinem Postfach nach."
    )


class ResendRequest(BaseModel):
    email: str


class VerifyRequest(BaseModel):
    email: str
    code: str


def _code_minutes() -> int:
    return int(accounts.CODE_TTL.total_seconds() // 60)


def _signup_allowed() -> None:
    if not get_settings().signup_enabled:
        raise HTTPException(
            status_code=403, detail="Die Registrierung ist derzeit geschlossen."
        )


def _throttle_codes(email: str, request: Request) -> None:
    key = (email, _client_ip(request))
    if code_throttle.blocked(*key):
        raise HTTPException(
            status_code=429,
            detail="Zu viele Anforderungen. Bitte in 15 Minuten erneut versuchen.",
        )
    code_throttle.record(*key)


@router.post("/register", response_model=CodeSent, status_code=202)
def register(request: RegisterRequest, http_request: Request) -> CodeSent:
    """Legt Mandant und ersten Nutzer an und schickt den Bestaetigungscode."""
    _signup_allowed()
    email = _normalized(request.email)
    if not EMAIL_PATTERN.match(email):
        raise HTTPException(
            status_code=400, detail="Bitte eine gueltige E-Mail-Adresse angeben."
        )
    _check_password(request.password)
    _throttle_codes(email, http_request)

    company = (request.company or "").strip() or email.split("@")[0]
    with session_scope() as session:
        existing = accounts.get_user_by_email(session, email)
        if existing is not None:
            # Nach aussen nicht von einer neuen Registrierung zu unterscheiden;
            # den Hinweis bekommt nur der Postfachinhaber.
            logger.info("Registrierung mit bereits vergebener Adresse %s", email)
            _send(send_already_registered, email)
            return CodeSent()

        user = accounts.create_tenant_with_owner(
            session, name=company, email=email, password=request.password
        )
        code = accounts.issue_code(session, user_id=user.id, purpose=CODE_SIGNUP)
        logger.info("Registrierung %s, neuer Mandant %s", email, user.tenant_id)

    _send(send_signup_code, email, code, _code_minutes())
    return CodeSent()


@router.post("/register/resend", response_model=CodeSent, status_code=202)
def resend_code(request: ResendRequest, http_request: Request) -> CodeSent:
    """Neuer Bestaetigungscode - der alte verfaellt dabei."""
    _signup_allowed()
    email = _normalized(request.email)
    _throttle_codes(email, http_request)

    with session_scope() as session:
        user = accounts.get_user_by_email(session, email)
        if user is None or user.verified_at is not None or not user.active:
            return CodeSent()
        code = accounts.issue_code(session, user_id=user.id, purpose=CODE_SIGNUP)

    _send(send_signup_code, email, code, _code_minutes())
    return CodeSent()


@router.post("/verify", response_model=SessionInfo)
def verify(
    request: VerifyRequest, response: Response, http_request: Request
) -> SessionInfo:
    """Bestaetigt die Adresse und meldet gleich an."""
    email = _normalized(request.email)
    attempt = (email, _client_ip(http_request))
    if login_throttle.blocked(*attempt):
        raise HTTPException(
            status_code=429,
            detail="Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen.",
        )

    # Der Fehlschlag wird erst nach dem Block gemeldet: eine Exception im
    # ``session_scope`` rollt die Transaktion zurueck - und damit den gerade
    # hochgezaehlten Fehlversuch am Code.
    with session_scope() as session:
        user = accounts.get_user_by_email(session, email)
        ok = (
            user is not None
            and user.active
            and user.tenant.active
            and accounts.redeem_code(
                session, user_id=user.id, purpose=CODE_SIGNUP, code=request.code
            )
        )
        current = None
        if ok:
            accounts.mark_verified(session, user)
            current = _current(user)

    if current is None:
        login_throttle.record(*attempt)
        raise HTTPException(
            status_code=400, detail="Der Code stimmt nicht oder ist abgelaufen."
        )

    login_throttle.clear(*attempt)
    _start_session(current.user_id, response)
    logger.info("Adresse bestaetigt: %s (Mandant %s)", current.email, current.tenant_id)
    return SessionInfo(
        authenticated=True, email=current.email, tenant_name=current.tenant_name
    )


# ---------------------------------------------------------------- Passwort vergessen


class ResetRequest(BaseModel):
    email: str


class ResetConfirmRequest(BaseModel):
    email: str
    code: str
    password: str


@router.post("/password-reset", response_model=CodeSent, status_code=202)
def request_password_reset(request: ResetRequest, http_request: Request) -> CodeSent:
    """Schickt einen Reset-Code - ohne zu verraten, ob es die Adresse gibt."""
    email = _normalized(request.email)
    _throttle_codes(email, http_request)

    with session_scope() as session:
        user = accounts.get_user_by_email(session, email)
        if user is None or not user.active or not user.tenant.active:
            logger.info("Reset fuer unbekannte oder gesperrte Adresse %s angefragt", email)
            return CodeSent()
        code = accounts.issue_code(
            session, user_id=user.id, purpose=CODE_PASSWORD_RESET
        )

    _send(send_reset_code, email, code, _code_minutes())
    return CodeSent()


@router.post("/password-reset/confirm", status_code=204)
def confirm_password_reset(
    request: ResetConfirmRequest, http_request: Request
) -> None:
    """Setzt das neue Passwort und wirft alle offenen Sitzungen raus."""
    email = _normalized(request.email)
    attempt = (email, _client_ip(http_request))
    if login_throttle.blocked(*attempt):
        raise HTTPException(
            status_code=429,
            detail="Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen.",
        )
    _check_password(request.password)

    with session_scope() as session:
        user = accounts.get_user_by_email(session, email)
        ok = (
            user is not None
            and user.active
            and user.tenant.active
            and accounts.redeem_code(
                session,
                user_id=user.id,
                purpose=CODE_PASSWORD_RESET,
                code=request.code,
            )
        )
        if ok:
            accounts.set_password(session, user, request.password)
            # Der Code beweist Zugriff auf das Postfach - damit gilt die Adresse
            # als bestaetigt, auch wenn die Registrierung nie fertig wurde.
            accounts.mark_verified(session, user)
            # Wer das alte Passwort hatte, fliegt raus: genau dafuer setzt man es zurueck.
            accounts.end_all_sessions(session, user.id)
            logger.info("Passwort zurueckgesetzt fuer %s", email)

    # Erst nach dem Commit melden - sonst verfiele der gezaehlte Fehlversuch
    # zusammen mit der Transaktion.
    if not ok:
        login_throttle.record(*attempt)
        raise HTTPException(
            status_code=400, detail="Der Code stimmt nicht oder ist abgelaufen."
        )

    login_throttle.clear(*attempt)


def _send(sender, *args) -> None:
    """Mailversand mit klarer Fehlermeldung statt Stacktrace im Browser."""
    try:
        sender(*args)
    except MailSendError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
