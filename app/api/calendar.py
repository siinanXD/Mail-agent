"""Belegungskalender und Buchungsdetail - je Mandant.

Der Kalender zeigt einen Monat: je Tag, welche Wohnungen belegt sind, wo eine
Buchung storniert wurde und wo es eine Umbuchung gab. Ein Klick auf den Tag
liefert die Buchungen des Tages, ein Klick auf die Buchung ihre ganze Geschichte.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.auth import CurrentUser, require_user
from app.database import repositories as repo
from app.reports.calendar import CalendarDay, MonthCalendar, build_month
from app.tenancy import tenant_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["kalender"])

MONATE = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)

#: Grenzen fuer die Blaetter-Navigation - schuetzt vor unsinnigen Jahreszahlen.
MIN_JAHR, MAX_JAHR = 2000, 2100


class DayBookingOut(BaseModel):
    booking_id: int
    booking_reference: str
    guest_name: str
    unit_id: int | None = None
    unit: str
    arrival_date: date
    departure_date: date
    status: str
    #: Anreise- bzw. Abreisetag - fuer die Halbtags-Darstellung im Kalender.
    arrival: bool = False
    departure: bool = False
    changed: bool = False
    cancelled: bool = False


class CalendarDayOut(BaseModel):
    date: date
    in_month: bool
    #: "frei", "belegt" oder "storniert"
    status: str
    changed: bool
    bookings: list[DayBookingOut]


class MonthOut(BaseModel):
    year: int
    month: int
    label: str
    first_day: date
    last_day: date
    today: date
    occupied_days: int
    previous: dict[str, int]
    next: dict[str, int]
    weeks: list[list[CalendarDayOut]]


class ChangeOut(BaseModel):
    changed_at: date
    field: str
    old_value: str | None = None
    new_value: str | None = None


class CancellationOut(BaseModel):
    cancelled_at: date
    reason: str | None = None


class BookingDetailOut(BaseModel):
    booking_id: int
    booking_reference: str
    guest_name: str
    arrival_date: date | None = None
    departure_date: date | None = None
    nights: int | None = None
    status: str
    unit_id: int | None = None
    unit: str | None = None
    source_email_id: int | None = None
    changes: list[ChangeOut] = []
    cancellation: CancellationOut | None = None


def _day_out(day: CalendarDay) -> CalendarDayOut:
    return CalendarDayOut(
        date=day.day,
        in_month=day.in_month,
        status=day.status,
        changed=day.changed,
        bookings=[
            DayBookingOut(
                booking_id=b.booking_id,
                booking_reference=b.booking_reference,
                guest_name=b.guest_name,
                unit_id=b.unit_id,
                unit=b.unit_name,
                arrival_date=b.arrival_date,
                departure_date=b.departure_date,
                status=b.status,
                arrival=b.arrival,
                departure=b.departure,
                changed=b.changed,
                cancelled=b.cancelled,
            )
            for b in day.bookings
        ],
    )


def _month_out(month: MonthCalendar, today: date) -> MonthOut:
    voriger = month.first_day - timedelta(days=1)
    naechster_monat = 1 if month.month == 12 else month.month + 1
    naechstes_jahr = month.year + 1 if month.month == 12 else month.year
    return MonthOut(
        year=month.year,
        month=month.month,
        label=f"{MONATE[month.month - 1]} {month.year}",
        first_day=month.first_day,
        last_day=month.last_day,
        today=today,
        occupied_days=month.occupied_days,
        previous={"year": voriger.year, "month": voriger.month},
        next={"year": naechstes_jahr, "month": naechster_monat},
        weeks=[[_day_out(day) for day in week] for week in month.weeks],
    )


@router.get("/calendar", response_model=MonthOut)
def calendar_month(
    year: int | None = Query(default=None, ge=MIN_JAHR, le=MAX_JAHR),
    month: int | None = Query(default=None, ge=1, le=12),
    user: CurrentUser = Depends(require_user),
) -> MonthOut:
    """Belegung eines Monats. Ohne Angabe: der laufende Monat."""
    today = date.today()
    with tenant_session(user.tenant_id) as session:
        kalender = build_month(session, year=year or today.year, month=month or today.month)
    return _month_out(kalender, today)


@router.get("/bookings/{booking_id}", response_model=BookingDetailOut)
def booking_detail(
    booking_id: int, user: CurrentUser = Depends(require_user)
) -> BookingDetailOut:
    """Alles zu einer Buchung: Zeitraum, Objekt, Umbuchungen, Stornierung."""
    with tenant_session(user.tenant_id) as session:
        booking = repo.get_booking(session, booking_id)
        if booking is None:
            raise HTTPException(status_code=404, detail="Buchung nicht gefunden")

        nights = None
        if booking.arrival_date and booking.departure_date:
            nights = (booking.departure_date - booking.arrival_date).days

        cancellation = repo.cancellation_for_booking(session, booking.id)
        return BookingDetailOut(
            booking_id=booking.id,
            booking_reference=booking.booking_reference,
            guest_name=booking.guest_name,
            arrival_date=booking.arrival_date,
            departure_date=booking.departure_date,
            nights=nights,
            status=booking.status,
            unit_id=booking.unit_id,
            unit=booking.unit.name if booking.unit else None,
            source_email_id=booking.source_email_id,
            changes=[
                ChangeOut(
                    changed_at=change.changed_at.date(),
                    field=change.field,
                    old_value=change.old_value,
                    new_value=change.new_value,
                )
                for change in repo.changes_for_booking(session, booking.id)
            ],
            cancellation=(
                CancellationOut(
                    cancelled_at=cancellation.cancelled_at.date(), reason=cancellation.reason
                )
                if cancellation
                else None
            ),
        )
