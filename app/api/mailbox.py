"""Postfach verbinden, Verbindung pruefen, dem Agenten bei der Arbeit zusehen.

Alles haengt am Mandanten der Sitzung - ein Nutzer sieht und aendert immer nur
das Postfach seines eigenen Mandanten, nie eines aus einem Parameter.

Das IMAP-Passwort geht nur in eine Richtung: herein zum Speichern (verschluesselt
per ``app.crypto``), nie wieder hinaus. Ein leeres Passwortfeld beim Speichern
heisst deshalb "unveraendert lassen" und nicht "Passwort loeschen".
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import CurrentUser, require_user
from app.config import get_settings
from app.crypto import EncryptionNotConfiguredError, decrypt_secret
from app.database import accounts
from app.database.connection import session_scope
from app.database.models import RUN_RUNNING, Mailbox
from app.email import watcher
from app.email.imap_client import CHECK_UNKNOWN, ImapConfig, check_connection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["mailbox"])

#: Ordner, die es bei den gaengigen Anbietern gibt - nur als Vorschlag.
COMMON_FOLDERS = ("INBOX", "INBOX/Buchungen", "Posteingang")


class MailboxSettings(BaseModel):
    """Was der Kunde einstellen kann."""

    host: str
    username: str
    #: Leer = bestehendes Passwort behalten. Beim ersten Anlegen Pflicht.
    password: str | None = None
    port: int = Field(default=993, ge=1, le=65535)
    folder: str = "INBOX"
    use_ssl: bool = True
    #: Ab wann der Agent ins Postfach schauen darf. Leer = alles im Ordner.
    since_date: date | None = None
    active: bool = True


class MailboxInfo(BaseModel):
    """Was die Oberflaeche anzeigt - ohne Passwort."""

    configured: bool
    host: str | None = None
    username: str | None = None
    port: int | None = None
    folder: str | None = None
    use_ssl: bool | None = None
    since_date: date | None = None
    active: bool | None = None
    #: ok / auth_error / tls_error / folder_missing / unreachable / unknown
    status: str = CHECK_UNKNOWN
    status_message: str | None = None
    last_check_at: str | None = None
    last_polled_at: str | None = None
    last_error: str | None = None
    next_run: str | None = None


class CheckResponse(BaseModel):
    status: str
    message: str
    ok: bool
    #: Muss jemand eingreifen (falsches Passwort), oder kann es sich von selbst
    #: erledigen (Server gerade weg)?
    permanent: bool = False
    #: Wie viele Mails ab dem gewaehlten Datum im Ordner liegen.
    waiting: int | None = None


class RunInfo(BaseModel):
    kind: str
    status: str
    started_at: str
    finished_at: str | None = None
    imported: int = 0
    skipped: int = 0
    bookings: int = 0
    cancellations: int = 0
    changes: int = 0
    failed: int = 0
    message: str | None = None


class ActivityResponse(BaseModel):
    #: Arbeitet der Agent gerade fuer diesen Mandanten?
    busy: bool
    next_run: str | None = None
    mailbox_status: str = CHECK_UNKNOWN
    runs: list[RunInfo] = []


def _stamp(value) -> str | None:
    return value.isoformat() if value else None


def _next_run() -> str | None:
    return _stamp(watcher.state.next_run)


def _info(mailbox: Mailbox | None) -> MailboxInfo:
    if mailbox is None:
        return MailboxInfo(configured=False, next_run=_next_run())
    return MailboxInfo(
        configured=True,
        host=mailbox.host,
        username=mailbox.username,
        port=mailbox.port,
        folder=mailbox.folder,
        use_ssl=mailbox.use_ssl,
        since_date=mailbox.since_date,
        active=mailbox.active,
        status=mailbox.status or CHECK_UNKNOWN,
        status_message=mailbox.status_message,
        last_check_at=_stamp(mailbox.last_check_at),
        last_polled_at=_stamp(mailbox.last_polled_at),
        last_error=mailbox.last_error,
        next_run=_next_run(),
    )


@router.get("/mailbox", response_model=MailboxInfo)
def read_mailbox(user: CurrentUser = Depends(require_user)) -> MailboxInfo:
    with session_scope() as session:
        return _info(accounts.get_mailbox(session, user.tenant_id))


@router.put("/mailbox", response_model=MailboxInfo)
def save_mailbox(
    settings: MailboxSettings, user: CurrentUser = Depends(require_user)
) -> MailboxInfo:
    """Speichert die Zugangsdaten und prueft die Verbindung sofort."""
    _require_fields(settings)
    _reject_private_target(settings.host.strip())
    with session_scope() as session:
        bestehend = accounts.get_mailbox(session, user.tenant_id)
        bestehend_id = bestehend.id if bestehend else None
    # Waehrend ein Abruf laeuft, arbeitet er mit der alten Konfiguration und
    # schriebe seinen Cursor ueber den zurueckgesetzten. Also warten lassen.
    schloss = watcher.mailbox_lock(bestehend_id) if bestehend_id is not None else None
    if schloss is not None and not schloss.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Gerade laeuft ein Abruf dieses Postfachs. Bitte gleich noch einmal speichern.",
        )
    try:
        with session_scope() as session:
            mailbox = accounts.save_mailbox(
                session,
                tenant_id=user.tenant_id,
                host=settings.host.strip(),
                username=settings.username.strip(),
                password=settings.password or None,
                port=settings.port,
                folder=settings.folder.strip() or "INBOX",
                use_ssl=settings.use_ssl,
                since_date=settings.since_date,
                active=settings.active,
            )
            mailbox_id = mailbox.id
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except EncryptionNotConfiguredError as error:
        # Ohne Schluessel laege das Passwort im Klartext in der Datenbank.
        raise HTTPException(status_code=503, detail=str(error)) from error
    finally:
        if schloss is not None:
            schloss.release()

    # Direkt nach dem Speichern pruefen: sonst stuende die Ampel bis zum
    # naechsten Testlauf auf "unbekannt", obwohl gerade jemand davorsitzt.
    try:
        watcher.check_mailbox(mailbox_id)
    except Exception:
        logger.exception("Verbindungstest nach dem Speichern fehlgeschlagen")

    with session_scope() as session:
        return _info(accounts.get_mailbox(session, user.tenant_id))


@router.post("/mailbox/test", response_model=CheckResponse)
def test_mailbox(
    settings: MailboxSettings | None = None,
    count_waiting: bool = False,
    user: CurrentUser = Depends(require_user),
) -> CheckResponse:
    """Prueft Zugangsdaten, ohne sie zu speichern.

    Ohne Rumpf wird das gespeicherte Postfach geprueft. Mit Rumpf und leerem
    Passwort wird das gespeicherte Passwort verwendet - so laesst sich ein
    anderer Ordner oder Server testen, ohne das Passwort erneut einzutippen.
    """
    with session_scope() as session:
        stored = accounts.get_mailbox(session, user.tenant_id)
        if settings is None:
            if stored is None:
                raise HTTPException(
                    status_code=404, detail="Es ist noch kein Postfach hinterlegt."
                )
            config = _stored_config(stored)
        else:
            _require_fields(settings, stored is not None)
            _reject_private_target(settings.host.strip())
            if not settings.password and stored is not None and (
                stored.host, stored.username
            ) != (settings.host.strip(), settings.username.strip()):
                raise HTTPException(
                    status_code=400,
                    detail="Fuer einen anderen Server oder Benutzernamen bitte das Passwort neu eingeben.",
                )
            password = settings.password or _stored_password(stored)
            config = ImapConfig(
                host=settings.host.strip(),
                username=settings.username.strip(),
                password=password,
                port=settings.port,
                folder=settings.folder.strip() or "INBOX",
                use_ssl=settings.use_ssl,
                since=settings.since_date,
            )

    result = check_connection(config, count_waiting=count_waiting)
    return CheckResponse(
        status=result.status,
        message=result.message,
        ok=result.ok,
        permanent=result.permanent,
        waiting=result.waiting,
    )


@router.post("/mailbox/poll", status_code=202)
def poll_now(
    background: BackgroundTasks, user: CurrentUser = Depends(require_user)
) -> dict:
    """Stoesst einen Abruf an und antwortet sofort.

    Bewusst nicht synchron: ein Rueckstau von tausend Mails braucht Minuten, und
    solange soll der Browser nicht warten. Was passiert, steht in /api/activity.
    """
    with session_scope() as session:
        mailbox = accounts.get_mailbox(session, user.tenant_id)
        if mailbox is None or not mailbox.active:
            raise HTTPException(
                status_code=404, detail="Es ist kein aktives Postfach hinterlegt."
            )
        mailbox_id = mailbox.id

    background.add_task(_poll_safely, user.tenant_id)
    return {"status": "started"}


def _poll_safely(tenant_id: int) -> None:
    # poll_tenant statt poll_mailbox: nur dort werden Putzplan-Aenderungen an
    # die Mitarbeiter gemeldet. Eine per "Jetzt abrufen" importierte
    # Stornierung erreichte die Reinigungskraft sonst erst beim naechsten
    # geplanten Abruf - oder nie, wenn der Watcher aus ist.
    try:
        watcher.poll_tenant(tenant_id)
    except Exception:
        # Der Fehler steht am Lauf und am Postfach; hier soll nur nichts
        # unbemerkt im Hintergrundtask verpuffen.
        logger.exception("Angestossener Abruf fuer Mandant %s fehlgeschlagen", tenant_id)


@router.get("/activity", response_model=ActivityResponse)
def activity(user: CurrentUser = Depends(require_user)) -> ActivityResponse:
    """Was der Agent zuletzt getan hat - und ob er gerade arbeitet."""
    with session_scope() as session:
        runs = accounts.recent_runs(session, tenant_id=user.tenant_id, limit=20)
        mailbox = accounts.get_mailbox(session, user.tenant_id)
        return ActivityResponse(
            busy=any(run.status == RUN_RUNNING for run in runs),
            next_run=_next_run(),
            mailbox_status=(mailbox.status if mailbox else CHECK_UNKNOWN) or CHECK_UNKNOWN,
            runs=[
                RunInfo(
                    kind=run.kind,
                    status=run.status,
                    started_at=run.started_at.isoformat(),
                    finished_at=_stamp(run.finished_at),
                    imported=run.imported,
                    skipped=run.skipped,
                    bookings=run.bookings,
                    cancellations=run.cancellations,
                    changes=run.changes,
                    failed=run.failed,
                    message=run.message,
                )
                for run in runs
            ],
        )


def _reject_private_target(host: str) -> None:
    """Kein Postfach auf internen Adressen - sonst wird "Verbindung testen" zur
    Sonde fuer alles, was im Netz des Servers lauscht (SSRF)."""
    if get_settings().allow_private_imap_hosts:
        return
    try:
        adressen = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return  # nicht aufloesbar - der Verbindungstest meldet das selbst
    for roh in adressen:
        adresse = ipaddress.ip_address(roh.split("%")[0])
        if (
            adresse.is_private
            or adresse.is_loopback
            or adresse.is_link_local
            or adresse.is_reserved
            or adresse.is_unspecified
        ):
            raise HTTPException(
                status_code=400,
                detail="Dieser Server liegt in einem internen Netz und kann nicht verwendet werden.",
            )


def _require_fields(settings: MailboxSettings, exists: bool = True) -> None:
    if not settings.host.strip() or not settings.username.strip():
        raise HTTPException(status_code=400, detail="Server und Benutzername sind Pflicht.")
    if not settings.password and not exists:
        raise HTTPException(
            status_code=400, detail="Fuer ein neues Postfach wird das Passwort gebraucht."
        )


def _stored_config(mailbox: Mailbox) -> ImapConfig:
    return watcher.mailbox_config(mailbox, password=_stored_password(mailbox))


def _stored_password(mailbox: Mailbox | None) -> str:
    if mailbox is None:
        raise HTTPException(
            status_code=400, detail="Fuer ein neues Postfach wird das Passwort gebraucht."
        )
    try:
        return decrypt_secret(mailbox.password_encrypted)
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
