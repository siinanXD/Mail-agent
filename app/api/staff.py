"""Mitarbeiter, ihre Wohnungen und der Putzplan-Versand per WhatsApp - je Mandant.

Wie ueberall kommt der Mandant aus der Sitzung. Fremde Mitarbeiter liefern 404,
fremde Wohnungen 400 - beides verraet nicht, ob es die ID anderswo gibt.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.api.auth import CurrentUser, require_user
from app.database import repositories as repo
from app.messaging import whatsapp
from app.messaging.whatsapp import MessagingNotConfiguredError
from app.staff import dispatcher
from app.staff.phone import InvalidPhoneError, normalize_phone
from app.staff.plan import (
    CleaningTask,
    next_slot,
    upcoming_week_start,
    week_label,
    week_plan,
    week_start_of,
)
from app.tenancy import tenant_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["mitarbeiter"])


# ---------------------------------------------------------------- Mitarbeiter


class UnitOut(BaseModel):
    id: int
    name: str


class StaffOut(BaseModel):
    id: int
    name: str
    phone: str
    active: bool
    units: list[UnitOut]


class StaffListResponse(BaseModel):
    staff: list[StaffOut]
    #: Alle erkannten Wohnungen des Mandanten - die Auswahl im Dropdown.
    units: list[UnitOut]


class StaffRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=1, max_length=40)
    unit_ids: list[int] = Field(default_factory=list)
    active: bool = True


def _staff_out(member) -> StaffOut:
    units = sorted((a.unit for a in member.assignments), key=lambda unit: unit.name.lower())
    return StaffOut(
        id=member.id,
        name=member.name,
        phone=member.phone,
        active=member.active,
        units=[UnitOut(id=unit.id, name=unit.name) for unit in units],
    )


def _cleaned(request: StaffRequest) -> tuple[str, str]:
    name = " ".join(request.name.split())
    if not name:
        raise HTTPException(status_code=400, detail="Bitte einen Namen angeben.")
    try:
        return name, normalize_phone(request.phone)
    except InvalidPhoneError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/staff", response_model=StaffListResponse)
def list_staff(user: CurrentUser = Depends(require_user)) -> StaffListResponse:
    with tenant_session(user.tenant_id) as session:
        return StaffListResponse(
            staff=[_staff_out(member) for member in repo.list_staff(session)],
            units=[UnitOut(id=unit.id, name=unit.name) for unit in repo.list_units(session)],
        )


@router.post("/staff", response_model=StaffOut, status_code=201)
def create_staff(request: StaffRequest, user: CurrentUser = Depends(require_user)) -> StaffOut:
    name, phone = _cleaned(request)
    try:
        with tenant_session(user.tenant_id) as session:
            member = repo.create_staff(
                session, name=name, phone=phone, unit_ids=request.unit_ids, active=request.active
            )
            return _staff_out(member)
    except repo.UnknownUnitError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.put("/staff/{staff_id}", response_model=StaffOut)
def update_staff(
    staff_id: int, request: StaffRequest, user: CurrentUser = Depends(require_user)
) -> StaffOut:
    name, phone = _cleaned(request)
    try:
        with tenant_session(user.tenant_id) as session:
            member = repo.get_staff(session, staff_id)
            if member is None:
                raise HTTPException(status_code=404, detail="Mitarbeiter nicht gefunden")
            repo.update_staff(
                session,
                member,
                name=name,
                phone=phone,
                unit_ids=request.unit_ids,
                active=request.active,
            )
            return _staff_out(member)
    except repo.UnknownUnitError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/staff/{staff_id}", status_code=204)
def delete_staff(staff_id: int, user: CurrentUser = Depends(require_user)) -> Response:
    with tenant_session(user.tenant_id) as session:
        member = repo.get_staff(session, staff_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Mitarbeiter nicht gefunden")
        repo.delete_staff(session, member)
    return Response(status_code=204)


# ---------------------------------------------------------------- Zeitplan


class ScheduleRequest(BaseModel):
    enabled: bool
    #: 0 = Montag ... 6 = Sonntag
    weekday: int = Field(ge=0, le=6)
    send_time: time


class WeekOption(BaseModel):
    week_start: date
    label: str


class ScheduleResponse(BaseModel):
    enabled: bool
    weekday: int
    send_time: str
    whatsapp_configured: bool
    #: Naechster Termin und die Woche, die dann verschickt wird.
    next_send_at: datetime | None = None
    next_week_start: date | None = None
    #: Auswahl fuer Vorschau und Versand von Hand: diese und naechste Woche.
    weeks: list[WeekOption]
    default_week_start: date


def _schedule_response(schedule) -> ScheduleResponse:
    now = dispatcher.current_time()
    enabled = bool(schedule and schedule.enabled)
    weekday = schedule.send_weekday if schedule else 6
    send_time = schedule.send_time if schedule else time(18, 0)
    next_at = next_slot(now, weekday, send_time) if enabled else None
    this_week = week_start_of(now.date())
    next_week = this_week + timedelta(days=7)
    return ScheduleResponse(
        enabled=enabled,
        weekday=weekday,
        send_time=send_time.strftime("%H:%M"),
        whatsapp_configured=whatsapp.whatsapp_configured(),
        next_send_at=next_at,
        next_week_start=upcoming_week_start(next_at.date()) if next_at else None,
        weeks=[
            WeekOption(week_start=this_week, label=f"Diese Woche · {week_label(this_week)}"),
            WeekOption(week_start=next_week, label=f"Nächste Woche · {week_label(next_week)}"),
        ],
        default_week_start=upcoming_week_start(now.date()),
    )


@router.get("/cleaning-schedule", response_model=ScheduleResponse)
def get_schedule(user: CurrentUser = Depends(require_user)) -> ScheduleResponse:
    with tenant_session(user.tenant_id) as session:
        return _schedule_response(repo.get_cleaning_schedule(session))


@router.put("/cleaning-schedule", response_model=ScheduleResponse)
def save_schedule(
    request: ScheduleRequest, user: CurrentUser = Depends(require_user)
) -> ScheduleResponse:
    with tenant_session(user.tenant_id) as session:
        schedule = repo.save_cleaning_schedule(
            session,
            enabled=request.enabled,
            weekday=request.weekday,
            send_time=request.send_time.replace(second=0, microsecond=0, tzinfo=None),
            now=dispatcher.current_time(),
        )
        return _schedule_response(schedule)


# ---------------------------------------------------------------- Vorschau und Versand


class TaskOut(BaseModel):
    day: date
    unit_id: int | None
    unit: str
    turnover: bool


class DispatchOut(BaseModel):
    kind: str
    success: bool
    created_at: datetime
    error: str | None = None


class StaffPlanOut(BaseModel):
    staff_id: int
    name: str
    phone: str
    tasks: list[TaskOut]
    last_dispatch: DispatchOut | None = None
    #: Seit der letzten zugestellten Nachricht hat sich ab heute etwas geaendert.
    changed_since_dispatch: bool = False


class PreviewResponse(BaseModel):
    week_start: date
    week_end: date
    label: str
    staff: list[StaffPlanOut]
    #: Reinigungen ohne zustaendigen Mitarbeiter
    unassigned: list[TaskOut]
    whatsapp_configured: bool


def _task_out(task: CleaningTask) -> TaskOut:
    return TaskOut(day=task.day, unit_id=task.unit_id, unit=task.unit_name, turnover=task.turnover)


def _monday(week_start: date | None) -> date:
    if week_start is None:
        return upcoming_week_start(dispatcher.current_time().date())
    if week_start.weekday() != 0:
        raise HTTPException(status_code=400, detail="week_start muss ein Montag sein.")
    return week_start


@router.get("/cleaning-schedule/preview", response_model=PreviewResponse)
def preview(
    week_start: date | None = Query(default=None),
    user: CurrentUser = Depends(require_user),
) -> PreviewResponse:
    """Wer in der Woche welche Wohnung putzt - so, wie es jetzt verschickt wuerde."""
    start = _monday(week_start)
    today = dispatcher.current_time().date()
    with tenant_session(user.tenant_id) as session:
        plan = week_plan(session, start)
        status = dispatcher.plan_status(session, plan, today)
        staff = []
        for staff_plan in plan.staff:
            last, changed = status[staff_plan.staff_id]
            staff.append(
                StaffPlanOut(
                    staff_id=staff_plan.staff_id,
                    name=staff_plan.name,
                    phone=staff_plan.phone,
                    tasks=[_task_out(task) for task in staff_plan.tasks],
                    last_dispatch=(
                        DispatchOut(
                            kind=last.kind,
                            success=last.success,
                            created_at=last.created_at,
                            error=last.error,
                        )
                        if last
                        else None
                    ),
                    changed_since_dispatch=changed,
                )
            )
        return PreviewResponse(
            week_start=start,
            week_end=plan.week_end,
            label=week_label(start),
            staff=staff,
            unassigned=[_task_out(task) for task in plan.unassigned],
            whatsapp_configured=whatsapp.whatsapp_configured(),
        )


class SendRequest(BaseModel):
    #: Montag der Woche - leer = die kommende Woche.
    week_start: date | None = None
    #: Nur an diesen Mitarbeiter - leer = an alle aktiven.
    staff_id: int | None = None


class SendOutcome(BaseModel):
    staff_id: int
    name: str
    success: bool
    task_count: int
    error: str | None = None


class SendResponse(BaseModel):
    week_start: date
    sent: int
    failed: int
    outcomes: list[SendOutcome]


@router.post("/cleaning-schedule/send", response_model=SendResponse)
def send(
    request: SendRequest | None = None, user: CurrentUser = Depends(require_user)
) -> SendResponse:
    """Putzplan jetzt verschicken - auch, wenn er diese Woche schon rausging."""
    request = request or SendRequest()
    start = _monday(request.week_start)
    try:
        outcomes = dispatcher.send_now(user.tenant_id, week_start=start, staff_id=request.staff_id)
    except MessagingNotConfiguredError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except dispatcher.StaffNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        logger.exception("Putzplan-Versand von Hand fehlgeschlagen")
        raise HTTPException(
            status_code=500,
            detail="Putzplan konnte nicht verschickt werden. Details stehen im Server-Log.",
        ) from error

    return SendResponse(
        week_start=start,
        sent=sum(outcome.success for outcome in outcomes),
        failed=sum(not outcome.success for outcome in outcomes),
        outcomes=[
            SendOutcome(
                staff_id=outcome.staff_id,
                name=outcome.name,
                success=outcome.success,
                task_count=outcome.task_count,
                error=outcome.error,
            )
            for outcome in outcomes
        ],
    )
