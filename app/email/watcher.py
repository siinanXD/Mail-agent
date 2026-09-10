"""Dauerbetrieb: pollt alle Postfaecher aller Mandanten zu festen Uhrzeiten.

Jedes Postfach wird einzeln abgearbeitet und in genau den Mandanten importiert,
dem es gehoert. Faellt ein Postfach aus (falsches Passwort, Server weg), laeuft
der Rest weiter - der Fehler steht am Postfach (``mailboxes.last_error``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.config import get_settings
from app.crypto import decrypt_secret
from app.database import accounts
from app.database.connection import session_scope
from app.database.models import Mailbox
from app.email.imap_client import ImapConfig, ImapNotConfiguredError, fetch_new_emails
from app.email.importer import ImportResult, import_emails
from app.tenancy import tenant_session

logger = logging.getLogger(__name__)

#: Nie laenger als eine Stunde am Stueck schlafen - so wirken Zeitumstellung
#: oder ein Suspend des Hosts sich spaetestens nach einer Stunde aus.
MAX_SLEEP_SECONDS = 3600


class WatcherState:
    """Sichtbarer Zustand des Pollings (fuer /health)."""

    def __init__(self) -> None:
        self.running = False
        self.last_run: datetime | None = None
        self.next_run: datetime | None = None
        self.last_error: str | None = None
        self.total_imported = 0
        self.total_skipped = 0


state = WatcherState()


@dataclass
class MailboxOutcome:
    mailbox_id: int
    tenant_id: int
    label: str
    result: ImportResult | None = None
    error: str | None = None


def next_run_at(now: datetime, schedule: list[time]) -> datetime:
    """Naechste Abrufzeit nach ``now``. Ohne Zeiten: morgen um Mitternacht."""
    if not schedule:
        return datetime.combine(now.date() + timedelta(days=1), time(0, 0))

    for slot in schedule:
        candidate = datetime.combine(now.date(), slot)
        if candidate > now:
            return candidate
    # Alle Zeiten des Tages sind durch -> erste Zeit am Folgetag.
    return datetime.combine(now.date() + timedelta(days=1), schedule[0])


def poll_mailbox(mailbox_id: int) -> MailboxOutcome:
    """Ein Postfach abrufen und in seinen Mandanten importieren."""
    batch_size = get_settings().poll_batch_size
    with session_scope() as session:
        mailbox = session.get(Mailbox, mailbox_id)
        if mailbox is None:
            raise ValueError(f"Postfach {mailbox_id} existiert nicht")
        outcome = MailboxOutcome(
            mailbox_id=mailbox.id,
            tenant_id=mailbox.tenant_id,
            label=f"{mailbox.username}@{mailbox.host}/{mailbox.folder}",
        )
        fields = {
            "host": mailbox.host,
            "username": mailbox.username,
            "port": mailbox.port,
            "folder": mailbox.folder,
            "use_ssl": mailbox.use_ssl,
            "since": mailbox.since_date,
            "encrypted": mailbox.password_encrypted,
        }

    try:
        config = ImapConfig(
            host=fields["host"],
            username=fields["username"],
            password=decrypt_secret(fields["encrypted"]),
            port=fields["port"],
            folder=fields["folder"],
            use_ssl=fields["use_ssl"],
            since=fields["since"],
            batch_size=batch_size,
        )
        emails = fetch_new_emails(config)
        with tenant_session(outcome.tenant_id) as session:
            outcome.result = import_emails(session, emails)
    except Exception as error:
        outcome.error = f"{error.__class__.__name__}: {error}"
        logger.exception("Postfach %s fehlgeschlagen", outcome.label)

    with session_scope() as session:
        accounts.mark_polled(session, mailbox_id, outcome.error)
    return outcome


def poll_all() -> list[MailboxOutcome]:
    """Alle aktiven Postfaecher aller aktiven Mandanten."""
    with session_scope() as session:
        ids = [mailbox.id for mailbox in accounts.active_mailboxes(session)]

    outcomes = [poll_mailbox(mailbox_id) for mailbox_id in ids]
    _record(outcomes)
    return outcomes


def poll_tenant(tenant_id: int) -> list[MailboxOutcome]:
    """Nur die Postfaecher eines Mandanten - fuer den manuellen Abruf."""
    with session_scope() as session:
        ids = [m.id for m in accounts.active_mailboxes(session, tenant_id=tenant_id)]
    if not ids:
        raise ImapNotConfiguredError(
            "Fuer diesen Mandanten ist kein aktives Postfach hinterlegt."
        )
    outcomes = [poll_mailbox(mailbox_id) for mailbox_id in ids]
    _record(outcomes)
    return outcomes


def _record(outcomes: list[MailboxOutcome]) -> None:
    state.last_run = datetime.now()
    errors = [f"{o.label}: {o.error}" for o in outcomes if o.error]
    state.last_error = "; ".join(errors) or None
    for outcome in outcomes:
        if outcome.result:
            state.total_imported += outcome.result.imported
            state.total_skipped += outcome.result.skipped


async def _loop() -> None:
    schedule = get_settings().poll_schedule
    logger.info(
        "Postfach-Watcher gestartet, Abruf um %s (lokale Serverzeit)",
        ", ".join(slot.strftime("%H:%M") for slot in schedule) or "-",
    )
    state.running = True

    try:
        while True:
            target = next_run_at(datetime.now(), schedule)
            state.next_run = target
            await _sleep_until(target)

            try:
                outcomes = await asyncio.to_thread(poll_all)
                if not outcomes:
                    logger.info("Keine aktiven Postfaecher hinterlegt.")
                for outcome in outcomes:
                    if outcome.result and (outcome.result.imported or outcome.result.failed):
                        logger.info(
                            "Mandant %s / %s: %d neu, %d uebersprungen, %d Fehler",
                            outcome.tenant_id,
                            outcome.label,
                            outcome.result.imported,
                            outcome.result.skipped,
                            len(outcome.result.failed),
                        )
            except Exception as error:
                # Ein Ausfall darf den Dienst nicht beenden.
                state.last_error = f"{error.__class__.__name__}: {error}"
                logger.exception("Postfach-Abruf fehlgeschlagen")
    except asyncio.CancelledError:
        logger.info("Postfach-Watcher gestoppt")
        raise
    finally:
        state.running = False
        state.next_run = None


async def _sleep_until(target: datetime) -> None:
    """Wartet bis ``target``, in Haeppchen von hoechstens einer Stunde."""
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, MAX_SLEEP_SECONDS))


def start(loop_task_holder: list) -> None:
    """Startet den Watcher. Welche Postfaecher er abruft, steht in der Datenbank."""
    if not get_settings().watch_enabled:
        logger.info("Postfach-Watcher ist per WATCH_ENABLED deaktiviert.")
        return
    loop_task_holder.append(asyncio.create_task(_loop(), name="mailbox-watcher"))


__all__ = [
    "ImapNotConfiguredError",
    "MailboxOutcome",
    "next_run_at",
    "poll_all",
    "poll_mailbox",
    "poll_tenant",
    "start",
    "state",
]
