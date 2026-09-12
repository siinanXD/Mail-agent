"""Dauerbetrieb: pollt alle Postfaecher aller Mandanten zu festen Uhrzeiten.

Jedes Postfach wird einzeln abgearbeitet und in genau den Mandanten importiert,
dem es gehoert. Faellt ein Postfach aus (falsches Passwort, Server weg), laeuft
der Rest weiter - der Fehler steht am Postfach (``mailboxes.last_error``).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.config import get_settings
from app.crypto import DecryptionError, decrypt_secret
from app.database import accounts
from app.database.connection import session_scope
from app.database.models import Mailbox
from app.database.models import RUN_ERROR, RUN_OK, RUN_POLL
from app.email.imap_client import (
    CHECK_AUTH_ERROR,
    CHECK_OK,
    ConnectionCheck,
    FetchResult,
    ImapConfig,
    ImapNotConfiguredError,
    check_connection,
    fetch_new_emails,
)
from app.email.importer import ImportResult, import_emails
from app.staff import dispatcher
from app.tenancy import tenant_session

logger = logging.getLogger(__name__)

#: Nie laenger als eine Stunde am Stueck schlafen - so wirken Zeitumstellung
#: oder ein Suspend des Hosts sich spaetestens nach einer Stunde aus.
MAX_SLEEP_SECONDS = 3600

#: Obergrenze fuer Batches je Postfach und Abruf. Ein riesiger Rueckstau wird so
#: ueber mehrere Abrufe verteilt, statt einen Lauf endlos zu blockieren.
MAX_BATCHES_PER_POLL = 20

#: So oft wird eine fehlgeschlagene Mail versucht (ein Versuch je Abruf), bevor
#: der Abruf an ihr vorbeigeht. Ohne Grenze wuerde eine dauerhaft kaputte Mail
#: das Postfach fuer immer blockieren, ohne Wiederholung ginge eine nur
#: voruebergehend gescheiterte Mail (LLM, Netz, Server) still verloren.
MAX_ATTEMPTS = 3

#: Ein Lauf, der so lange als "laeuft" dasteht, wurde von einem Neustart
#: erwischt. Er wird als abgebrochen markiert, sonst dreht sich die Anzeige ewig.
STALE_RUN_AFTER = timedelta(hours=1)


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
    #: Nur bei einem Fehlschlag gefuellt: warum der Abruf scheiterte, in einem
    #: Satz, den die Oberflaeche zeigen kann.
    check: ConnectionCheck | None = None


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


#: Ein Postfach wird nie zweimal gleichzeitig abgerufen - etwa Zeitplan und
#: POST /emails/poll, oder zwei Nutzer desselben Mandanten. Sonst holen beide
#: denselben Batch, importieren ihn doppelt, und der Cursor des einen
#: ueberschreibt den des anderen - bis an einer wartenden Mail vorbei.
#: Gilt je Prozess; es pollt ohnehin nur eine Instanz (README).
_mailbox_locks: dict[int, threading.Lock] = {}
_mailbox_locks_guard = threading.Lock()

BUSY_MESSAGE = "Abruf laeuft bereits - dieser Abruf wurde uebersprungen"


def mailbox_lock(mailbox_id: int) -> threading.Lock:
    """Das Schloss eines Postfachs - auch fuer die Einstellungen, damit niemand
    Server oder Cursor aendert, waehrend ein Abruf damit arbeitet."""
    with _mailbox_locks_guard:
        return _mailbox_locks.setdefault(mailbox_id, threading.Lock())


def poll_mailbox(mailbox_id: int) -> MailboxOutcome:
    """Ein Postfach abrufen und in seinen Mandanten importieren."""
    with _mailbox_locks_guard:
        lock = _mailbox_locks.setdefault(mailbox_id, threading.Lock())
    if not lock.acquire(blocking=False):
        logger.info("Postfach %s wird schon abgerufen - uebersprungen", mailbox_id)
        return _busy_outcome(mailbox_id)
    try:
        return _poll(mailbox_id)
    finally:
        lock.release()


def _busy_outcome(mailbox_id: int) -> MailboxOutcome:
    """Ergebnis fuer einen uebersprungenen Abruf - ohne last_error anzufassen,
    der gehoert dem laufenden Abruf."""
    with session_scope() as session:
        mailbox = session.get(Mailbox, mailbox_id)
        if mailbox is None:
            raise ValueError(f"Postfach {mailbox_id} existiert nicht")
        return MailboxOutcome(
            mailbox_id=mailbox.id,
            tenant_id=mailbox.tenant_id,
            label=_label(mailbox),
            error=BUSY_MESSAGE,
        )


def _label(mailbox: Mailbox) -> str:
    return f"{mailbox.username}@{mailbox.host}/{mailbox.folder}"


def _poll(mailbox_id: int) -> MailboxOutcome:
    batch_size = get_settings().poll_batch_size
    with session_scope() as session:
        mailbox = session.get(Mailbox, mailbox_id)
        if mailbox is None:
            raise ValueError(f"Postfach {mailbox_id} existiert nicht")
        outcome = MailboxOutcome(
            mailbox_id=mailbox.id,
            tenant_id=mailbox.tenant_id,
            label=_label(mailbox),
        )
        fields = {
            "host": mailbox.host,
            "username": mailbox.username,
            "port": mailbox.port,
            "folder": mailbox.folder,
            "use_ssl": mailbox.use_ssl,
            "since": mailbox.since_date,
            "encrypted": mailbox.password_encrypted,
            "last_uid": mailbox.last_uid,
            "uid_validity": mailbox.uid_validity,
            "retry_uid": mailbox.retry_uid,
            "retry_count": mailbox.retry_count or 0,
        }

    with session_scope() as session:
        run_id = accounts.start_run(
            session, tenant_id=outcome.tenant_id, mailbox_id=mailbox_id, kind=RUN_POLL
        )

    password = ""
    try:
        password = decrypt_secret(fields["encrypted"])
        last_uid, uid_validity = fields["last_uid"], fields["uid_validity"]
        retry_uid, retry_count = fields["retry_uid"], fields["retry_count"]
        total = ImportResult()
        for _ in range(MAX_BATCHES_PER_POLL):
            fetched = fetch_new_emails(
                ImapConfig(
                    host=fields["host"],
                    username=fields["username"],
                    password=password,
                    port=fields["port"],
                    folder=fields["folder"],
                    use_ssl=fields["use_ssl"],
                    since=fields["since"],
                    batch_size=batch_size,
                    last_uid=last_uid,
                    uid_validity=uid_validity,
                )
            )
            with tenant_session(outcome.tenant_id) as session:
                batch = import_emails(session, fetched.emails)
            _add_result(total, batch)

            # Cursor erst nach dem Import weiterschieben - und nie an einer Mail
            # vorbei, die nicht ankam oder nicht importiert werden konnte. Bricht
            # der Import ganz ab (Exception), bleibt der Cursor ohnehin stehen.
            if (
                uid_validity is not None
                and fetched.uid_validity is not None
                and fetched.uid_validity != uid_validity
            ):
                # Der Server hat die UIDs neu vergeben: fetch_new_emails faengt
                # dann wieder bei 1 an. Ein gemerkter Wiederholungsversuch zeigt
                # jetzt auf eine voellig andere Mail - die waere nach einem
                # einzigen Fehlschlag uebersprungen. Also von vorn zaehlen.
                retry_uid, retry_count = None, 0
            uid_validity = fetched.uid_validity
            last_uid = fetched.last_uid
            failed = _failed_uids(fetched, batch)
            blocked = False
            if failed:
                first = failed[0]
                attempts = retry_count + 1 if retry_uid == first else 1
                if attempts < MAX_ATTEMPTS:
                    last_uid, retry_uid, retry_count = first - 1, first, attempts
                    blocked = True
                    outcome.error = (
                        f"{len(failed)} Mail(s) ab UID {first} fehlgeschlagen, "
                        f"Versuch {attempts}/{MAX_ATTEMPTS} - wird beim naechsten Abruf wiederholt"
                    )
                else:
                    # Aufgegeben wird nur die UID, deren Versuche gezaehlt wurden.
                    # Spaetere Fehler desselben Batches haben ihre eigenen Versuche
                    # noch vor sich - sonst gingen sie ungezaehlt mit verloren.
                    skipped = f"UID {first} nach {MAX_ATTEMPTS} Versuchen uebersprungen"
                    logger.error("Postfach %s: %s", outcome.label, skipped)
                    later = [uid for uid in failed if uid > first]
                    if later:
                        last_uid, retry_uid, retry_count = later[0] - 1, later[0], 1
                        blocked = True
                        outcome.error = (
                            f"{skipped}; {len(later)} weitere Mail(s) ab UID {later[0]} "
                            f"fehlgeschlagen, Versuch 1/{MAX_ATTEMPTS}"
                        )
                    else:
                        retry_uid, retry_count = None, 0
                        outcome.error = skipped
            else:
                retry_uid, retry_count = None, 0

            with session_scope() as session:
                accounts.save_cursor(
                    session,
                    mailbox_id,
                    uid_validity=uid_validity,
                    last_uid=last_uid,
                    retry_uid=retry_uid,
                    retry_count=retry_count,
                )
            # Blockiert: spaetere Batches warten, bis die Mail durch ist oder
            # aufgegeben wurde - sonst liefe der Cursor doch an ihr vorbei.
            if blocked or fetched.remaining == 0:
                break
        outcome.result = total
    except Exception as error:
        outcome.error = f"{error.__class__.__name__}: {error}"
        logger.exception("Postfach %s fehlgeschlagen", outcome.label)
        # Woran lag es? Die Ausnahme aus dem Abruf ist fuer den Kunden nicht
        # lesbar ("gaierror: [Errno 11001] ..."), und ob jemand eingreifen muss,
        # sieht man ihr auch nicht an. Ein Verbindungstest klaert beides - und
        # nur im Fehlerfall, also ohne Kosten im Normalbetrieb.
        # Ohne Passwort gab es nie eine Verbindung - dann sagt ein Test nichts.
        if password:
            outcome.check = check_connection(
                ImapConfig(
                    host=fields["host"],
                    username=fields["username"],
                    password=password,
                    port=fields["port"],
                    folder=fields["folder"],
                    use_ssl=fields["use_ssl"],
                )
            )

    with session_scope() as session:
        accounts.mark_polled(session, mailbox_id, outcome.error)
        # Ein gelungener Abruf ist zugleich der beste Verbindungsnachweis.
        if outcome.result is not None and outcome.error is None:
            accounts.record_check(
                session, mailbox_id, status=CHECK_OK, message="Verbindung steht."
            )
        elif outcome.check is not None:
            accounts.record_check(
                session,
                mailbox_id,
                status=outcome.check.status,
                message=outcome.check.message,
            )
        accounts.finish_run(
            session,
            run_id,
            status=RUN_OK if outcome.error is None else RUN_ERROR,
            message=_failure_message(outcome) or _summary(outcome.result),
            imported=outcome.result.imported if outcome.result else 0,
            skipped=outcome.result.skipped if outcome.result else 0,
            bookings=outcome.result.bookings if outcome.result else 0,
            cancellations=outcome.result.cancellations if outcome.result else 0,
            changes=outcome.result.changes if outcome.result else 0,
            failed=len(outcome.result.failed) if outcome.result else 0,
        )
    return outcome


def _failure_message(outcome: MailboxOutcome) -> str | None:
    """Der Fehler in der Sprache des Kunden - die Technik steht im Log."""
    if outcome.error is None:
        return None
    if outcome.check is not None and not outcome.check.ok:
        return outcome.check.message
    return outcome.error


def _summary(result: ImportResult | None) -> str:
    """Kurze Bilanz fuer die Aktivitaetsanzeige - in der Sprache des Kunden."""
    if result is None:
        return "Abruf beendet."
    if not result.imported:
        return "Keine neuen Mails."
    teile = [f"{result.imported} neue Mail(s)"]
    for zahl, wort in (
        (result.bookings, "Buchung(en)"),
        (result.cancellations, "Stornierung(en)"),
        (result.changes, "Umbuchung(en)"),
    ):
        if zahl:
            teile.append(f"{zahl} {wort}")
    return ", ".join(teile) + " erkannt." if len(teile) > 1 else teile[0] + " gelesen."


def _add_result(total: ImportResult, batch: ImportResult) -> None:
    for field in ("imported", "skipped", "bookings", "cancellations", "changes", "units", "chunks"):
        setattr(total, field, getattr(total, field) + getattr(batch, field))
    total.failed.extend(batch.failed)
    total.failed_message_ids.extend(batch.failed_message_ids)


def _failed_uids(fetched: FetchResult, batch: ImportResult) -> list[int]:
    """Aufsteigend: nicht ausgelieferte UIDs plus die UIDs gescheiterter Importe."""
    failed = set(fetched.failed_uids)
    failed.update(
        fetched.uids[message_id]
        for message_id in batch.failed_message_ids
        if message_id in fetched.uids
    )
    return sorted(failed)


def poll_all() -> list[MailboxOutcome]:
    """Alle aktiven Postfaecher aller aktiven Mandanten."""
    with session_scope() as session:
        ids = [mailbox.id for mailbox in accounts.active_mailboxes(session)]

    outcomes = [poll_mailbox(mailbox_id) for mailbox_id in ids]
    _record(outcomes)
    # Neue Stornos und Umbuchungen koennen verschickte Putzplaene betreffen.
    dispatcher.notify_changes_safely(outcome.tenant_id for outcome in outcomes)
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
    # Neue Stornos und Umbuchungen koennen verschickte Putzplaene betreffen.
    dispatcher.notify_changes_safely(outcome.tenant_id for outcome in outcomes)
    return outcomes


# ---------------------------------------------------------------- Verbindungstest


def mailbox_config(mailbox: Mailbox, *, password: str) -> ImapConfig:
    """Verbindungsdaten eines Postfachs - ohne Cursor, fuer den reinen Test."""
    return ImapConfig(
        host=mailbox.host,
        username=mailbox.username,
        password=password,
        port=mailbox.port,
        folder=mailbox.folder,
        use_ssl=mailbox.use_ssl,
        since=mailbox.since_date,
    )


def check_mailbox(mailbox_id: int, *, count_waiting: bool = False) -> ConnectionCheck:
    """Prueft ein Postfach und schreibt das Ergebnis an ihm fest."""
    with session_scope() as session:
        mailbox = session.get(Mailbox, mailbox_id)
        if mailbox is None:
            raise ValueError(f"Postfach {mailbox_id} existiert nicht")
        try:
            config = mailbox_config(mailbox, password=decrypt_secret(mailbox.password_encrypted))
        except DecryptionError as error:
            # Kein Netzwerkproblem: der Schluessel passt nicht mehr zum Gespeicherten.
            result = ConnectionCheck(CHECK_AUTH_ERROR, str(error))
            accounts.record_check(
                session, mailbox_id, status=result.status, message=result.message
            )
            return result

    result = check_connection(config, count_waiting=count_waiting)
    with session_scope() as session:
        accounts.record_check(
            session, mailbox_id, status=result.status, message=result.message
        )
    if not result.ok:
        logger.warning(
            "Postfach %s: %s (%s)", config, result.message, result.status
        )
    return result


def check_all() -> None:
    """Alle aktiven Postfaecher pruefen - ein Ausfall stoppt die anderen nicht."""
    with session_scope() as session:
        ids = [mailbox.id for mailbox in accounts.active_mailboxes(session)]
    for mailbox_id in ids:
        try:
            check_mailbox(mailbox_id)
        except Exception:
            logger.exception("Verbindungstest fuer Postfach %s fehlgeschlagen", mailbox_id)


async def _check_loop() -> None:
    """Haelt den Verbindungsstatus aktuell, unabhaengig vom Abrufplan."""
    minutes = max(1, get_settings().connection_check_minutes)
    logger.info("Verbindungstest alle %d Minuten", minutes)
    try:
        while True:
            try:
                await asyncio.to_thread(check_all)
                # Ein Neustart mitten im Abruf laesst Laeufe als "laeuft" stehen.
                await asyncio.to_thread(_abandon_stale)
            except Exception:
                logger.exception("Verbindungstest fehlgeschlagen")
            await asyncio.sleep(minutes * 60)
    except asyncio.CancelledError:
        logger.info("Verbindungstest gestoppt")
        raise


def _abandon_stale() -> None:
    with session_scope() as session:
        accounts.abandon_stale_runs(session, older_than=STALE_RUN_AFTER)


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
    loop_task_holder.append(asyncio.create_task(_check_loop(), name="mailbox-check"))


__all__ = [
    "ImapNotConfiguredError",
    "MailboxOutcome",
    "check_all",
    "check_mailbox",
    "mailbox_config",
    "next_run_at",
    "poll_all",
    "poll_mailbox",
    "poll_tenant",
    "start",
    "state",
]
