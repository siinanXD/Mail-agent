"""Versand des Putzplans an die Mitarbeiter per WhatsApp.

Drei Wege fuehren hierher:

* **Zeitplan** (``_loop``): Alle fuenf Minuten wird je Mandant geprueft, ob der
  eingestellte Versandtermin erreicht ist. Dann bekommt jeder aktive Mitarbeiter
  den Plan der kommenden Woche - genau einmal, auch ueber Neustarts hinweg, denn
  verschickt ist, was in ``cleaning_dispatches`` steht.
* **Aenderungen** (``notify_changes``) nach jedem Mail-Abruf: Der zuletzt
  zugestellte Stand wird mit dem aktuellen verglichen. Nur wer ab heute eine
  andere Reinigung hat, bekommt eine Nachricht.
* **Von Hand** (``send_now``) aus der Oberflaeche.

Jede Nachricht wird sofort nach dem Versand festgeschrieben. Sonst bekaemen nach
einem Absturz mitten im Durchlauf alle davor den Plan beim naechsten Mal doppelt.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import accounts
from app.database import repositories as repo
from app.database.connection import session_scope
from app.database.models import CleaningDispatch, Tenant
from app.messaging import whatsapp
from app.messaging.whatsapp import MessagingNotConfiguredError, OutgoingMessage, Sender
from app.staff.plan import (
    CleaningTask,
    StaffPlan,
    TaskDiff,
    WeekPlan,
    diff_tasks,
    due_week,
    update_message,
    week_plan,
    weekly_message,
)
from app.tenancy import tenant_id_for, tenant_session

logger = logging.getLogger(__name__)

#: Wie beim Postfach-Abruf: So oft wird eine gescheiterte Nachricht automatisch
#: versucht, danach nur noch von Hand. Ohne Grenze bekaeme Twilio fuer eine falsche
#: Nummer alle fuenf Minuten dieselbe Anfrage.
MAX_ATTEMPTS = 3

#: Abstand der Pruefungen - der Plan geht bis zu fuenf Minuten nach dem Termin raus.
TICK_SECONDS = 300

MAX_ERROR_LENGTH = 500


class StaffNotFoundError(LookupError):
    """Den Mitarbeiter gibt es nicht, er ist inaktiv oder gehoert einem anderen Mandanten."""


class DispatcherState:
    """Sichtbarer Zustand des Zeitplans (fuer /health)."""

    def __init__(self) -> None:
        self.running = False
        self.last_run: datetime | None = None


state = DispatcherState()


@dataclass
class DispatchOutcome:
    staff_id: int
    name: str
    kind: str
    success: bool
    task_count: int = 0
    error: str | None = None


def current_time() -> datetime:
    """Lokale Serverzeit - wie die Abrufzeiten des Postfachs (``TZ``)."""
    return datetime.now()


#: Ein Mandant wird nie gleichzeitig vom Zeitplan, nach einem Mail-Abruf und aus
#: der Oberflaeche beliefert - sonst saehen zwei Durchlaeufe "noch nicht
#: verschickt" und schickten beide. Gilt je Prozess, wie beim Postfach-Abruf.
_tenant_locks: dict[int, threading.Lock] = {}
_tenant_locks_guard = threading.Lock()


def _tenant_lock(tenant_id: int) -> threading.Lock:
    with _tenant_locks_guard:
        return _tenant_locks.setdefault(tenant_id, threading.Lock())


def _tenant_name(session: Session) -> str:
    tenant = session.get(Tenant, tenant_id_for(session))
    return tenant.name if tenant else ""


def _history(session: Session, week_start: date) -> dict[int, list[CleaningDispatch]]:
    """Nachrichten der Woche je Mitarbeiter, aelteste zuerst."""
    by_staff: dict[int, list[CleaningDispatch]] = {}
    for dispatch in repo.dispatches_for_week(session, week_start):
        by_staff.setdefault(dispatch.staff_id, []).append(dispatch)
    return by_staff


def _last_success(history: list[CleaningDispatch]) -> CleaningDispatch | None:
    return next((dispatch for dispatch in reversed(history) if dispatch.success), None)


def _failures_since_success(history: list[CleaningDispatch]) -> int:
    count = 0
    for dispatch in reversed(history):
        if dispatch.success:
            break
        count += 1
    return count


def _pending_diff(last: CleaningDispatch, plan: StaffPlan, today: date) -> TaskDiff:
    """Was sich seit der letzten zugestellten Nachricht ab heute geaendert hat.

    Vergangene Tage zaehlen nicht - eine Umbuchung von gestern braucht niemand mehr.
    """
    sent = [CleaningTask.from_json(item) for item in last.tasks]
    return diff_tasks(
        [task for task in sent if task.day >= today],
        [task for task in plan.tasks if task.day >= today],
    )


def _deliver(
    session: Session,
    *,
    plan: StaffPlan,
    week_start: date,
    kind: str,
    body: str,
    variables: dict[str, str],
    task_count: int,
    now: datetime,
    sender: Sender,
) -> DispatchOutcome:
    outcome = DispatchOutcome(
        staff_id=plan.staff_id, name=plan.name, kind=kind, success=False, task_count=task_count
    )
    provider_message_id = None
    try:
        sent = sender(
            OutgoingMessage(to=plan.phone, body=body, variables=variables, template=kind)
        )
    except MessagingNotConfiguredError:
        raise
    except Exception as error:
        outcome.error = str(error)[:MAX_ERROR_LENGTH] or error.__class__.__name__
        # Ohne den Fehlertext: Twilio nennt darin die Telefonnummer.
        logger.warning(
            "Putzplan an Mitarbeiter %s nicht zugestellt (%s)",
            plan.staff_id,
            error.__class__.__name__,
        )
    else:
        outcome.success = True
        provider_message_id = sent.provider_message_id

    repo.add_dispatch(
        session,
        staff_id=plan.staff_id,
        week_start=week_start,
        kind=kind,
        success=outcome.success,
        tasks=[task.to_json() for task in plan.tasks],
        created_at=now,
        provider_message_id=provider_message_id,
        error=outcome.error,
    )
    session.commit()
    return outcome


def send_week(
    session: Session,
    *,
    week_start: date,
    now: datetime,
    sender: Sender,
    staff_id: int | None = None,
    only_missing: bool = False,
) -> list[DispatchOutcome]:
    """Wochenplan an die aktiven Mitarbeiter - Reinigungen ab heute.

    ``only_missing`` (Zeitplan): nur, wer fuer die Woche noch nichts zugestellt
    bekommen hat, und hoechstens ``MAX_ATTEMPTS`` Versuche. Von Hand wird immer
    gesendet; der neue Stand gilt danach als verschickt.
    """
    plan = week_plan(session, week_start)
    history = _history(session, week_start)
    tenant_name = _tenant_name(session)
    outcomes = []
    for staff_plan in plan.staff:
        if staff_id is not None and staff_plan.staff_id != staff_id:
            continue
        mine = history.get(staff_plan.staff_id, [])
        if only_missing and (
            _last_success(mine) is not None or _failures_since_success(mine) >= MAX_ATTEMPTS
        ):
            continue
        visible = [task for task in staff_plan.tasks if task.day >= now.date()]
        body, variables = weekly_message(
            tenant_name=tenant_name,
            staff_name=staff_plan.name,
            week_start=week_start,
            tasks=visible,
        )
        outcomes.append(
            _deliver(
                session,
                plan=staff_plan,
                week_start=week_start,
                kind="plan",
                body=body,
                variables=variables,
                task_count=len(visible),
                now=now,
                sender=sender,
            )
        )
    return outcomes


def send_now(
    tenant_id: int,
    *,
    week_start: date,
    staff_id: int | None = None,
    sender: Sender | None = None,
) -> list[DispatchOutcome]:
    """Versand von Hand - an alle aktiven Mitarbeiter oder an einen."""
    if sender is None:
        if not whatsapp.whatsapp_configured():
            raise MessagingNotConfiguredError(
                "WhatsApp-Versand ist nicht eingerichtet "
                "(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM)."
            )
        sender = whatsapp.send_whatsapp

    with _tenant_lock(tenant_id), tenant_session(tenant_id) as session:
        if staff_id is not None:
            member = repo.get_staff(session, staff_id)
            if member is None or not member.active:
                raise StaffNotFoundError("Mitarbeiter nicht gefunden oder inaktiv")
        return send_week(
            session, week_start=week_start, now=current_time(), sender=sender, staff_id=staff_id
        )


def run_due(*, now: datetime | None = None, sender: Sender | None = None) -> list[DispatchOutcome]:
    """Ein Durchlauf des Zeitplans ueber alle aktiven Mandanten."""
    now = now or current_time()
    state.last_run = now
    if sender is None:
        if not whatsapp.whatsapp_configured():
            return []
        sender = whatsapp.send_whatsapp

    with session_scope() as session:
        tenant_ids = [tenant.id for tenant in accounts.list_tenants(session) if tenant.active]

    outcomes: list[DispatchOutcome] = []
    for tenant_id in tenant_ids:
        try:
            outcomes += _run_due_for(tenant_id, now, sender)
        except Exception:
            # Ein Mandant darf den Versand der anderen nicht aufhalten.
            logger.exception("Putzplan-Versand fuer Mandant %s fehlgeschlagen", tenant_id)
    return outcomes


def _run_due_for(tenant_id: int, now: datetime, sender: Sender) -> list[DispatchOutcome]:
    with _tenant_lock(tenant_id), tenant_session(tenant_id) as session:
        schedule = repo.get_cleaning_schedule(session)
        if schedule is None or not schedule.enabled:
            return []
        week_start = due_week(
            now,
            weekday=schedule.send_weekday,
            send_time=schedule.send_time,
            active_since=schedule.active_since,
        )
        if week_start is None:
            return []
        outcomes = send_week(
            session, week_start=week_start, now=now, sender=sender, only_missing=True
        )

    if outcomes:
        logger.info(
            "Mandant %s: Putzplan ab %s an %d Mitarbeiter verschickt, %d fehlgeschlagen",
            tenant_id,
            week_start,
            sum(outcome.success for outcome in outcomes),
            sum(not outcome.success for outcome in outcomes),
        )
    return outcomes


def notify_changes(
    tenant_id: int, *, now: datetime | None = None, sender: Sender | None = None
) -> list[DispatchOutcome]:
    """Aenderungen an bereits verschickten Plaenen an die betroffenen Mitarbeiter."""
    if sender is None:
        if not whatsapp.whatsapp_configured():
            return []
        sender = whatsapp.send_whatsapp
    now = now or current_time()
    today = now.date()

    outcomes: list[DispatchOutcome] = []
    with _tenant_lock(tenant_id), tenant_session(tenant_id) as session:
        tenant_name = _tenant_name(session)
        # Nur Wochen, die noch nicht vorbei sind.
        for week_start in repo.dispatched_weeks(session, since=today - timedelta(days=6)):
            plan = week_plan(session, week_start)
            history = _history(session, week_start)
            for staff_plan in plan.staff:
                mine = history.get(staff_plan.staff_id, [])
                last = _last_success(mine)
                # Wer den Plan nie bekommen hat, braucht auch keine Aenderung.
                if last is None or _failures_since_success(mine) >= MAX_ATTEMPTS:
                    continue
                diff = _pending_diff(last, staff_plan, today)
                if diff.empty:
                    continue
                visible = [task for task in staff_plan.tasks if task.day >= today]
                body, variables = update_message(
                    tenant_name=tenant_name,
                    staff_name=staff_plan.name,
                    week_start=week_start,
                    diff=diff,
                    tasks=visible,
                )
                outcomes.append(
                    _deliver(
                        session,
                        plan=staff_plan,
                        week_start=week_start,
                        kind="update",
                        body=body,
                        variables=variables,
                        task_count=len(visible),
                        now=now,
                        sender=sender,
                    )
                )
    return outcomes


def notify_changes_safely(tenant_ids: Iterable[int]) -> None:
    """Nach einem Mail-Abruf oder Import - ein Versandfehler laesst den Abruf nicht scheitern."""
    for tenant_id in sorted(set(tenant_ids)):
        try:
            outcomes = notify_changes(tenant_id)
        except Exception:
            logger.exception("Putzplan-Aenderungen fuer Mandant %s nicht verschickt", tenant_id)
            continue
        if outcomes:
            logger.info(
                "Mandant %s: %d Putzplan-Aenderung(en) verschickt", tenant_id, len(outcomes)
            )


def plan_status(
    session: Session, plan: WeekPlan, today: date
) -> dict[int, tuple[CleaningDispatch | None, bool]]:
    """Je Mitarbeiter: letzte Nachricht der Woche und ob sich seit der letzten
    zugestellten etwas geaendert hat - fuer die Vorschau."""
    history = _history(session, plan.week_start)
    status = {}
    for staff_plan in plan.staff:
        mine = history.get(staff_plan.staff_id, [])
        last = _last_success(mine)
        changed = last is not None and not _pending_diff(last, staff_plan, today).empty
        status[staff_plan.staff_id] = (mine[-1] if mine else None, changed)
    return status


async def _loop() -> None:
    logger.info("Putzplan-Versand gestartet, Pruefung alle %d Minuten", TICK_SECONDS // 60)
    state.running = True
    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await asyncio.to_thread(run_due)
            except Exception:
                logger.exception("Putzplan-Versand fehlgeschlagen")
    except asyncio.CancelledError:
        logger.info("Putzplan-Versand gestoppt")
        raise
    finally:
        state.running = False


def start(loop_task_holder: list) -> None:
    """Startet den Zeitplan - nur, wenn WhatsApp eingerichtet ist."""
    if not get_settings().cleaning_dispatch_enabled:
        logger.info("Putzplan-Versand ist per CLEANING_DISPATCH_ENABLED deaktiviert.")
        return
    if not whatsapp.whatsapp_configured():
        logger.info(
            "WhatsApp ist nicht eingerichtet (TWILIO_*) - der Putzplan wird nicht verschickt."
        )
        return
    loop_task_holder.append(asyncio.create_task(_loop(), name="cleaning-dispatcher"))
