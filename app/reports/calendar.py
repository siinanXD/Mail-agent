"""Belegungskalender: ein Monat, ein Feld je Tag.

Ein Tag ist belegt vom Anreisetag bis **vor** dem Abreisetag - dieselbe Regel wie
in ``app.reports.occupancy`` und im Putzplan. Am Abreisetag kann also schon der
naechste Gast anreisen.

Farben wie im Verlauf:

    weiss   nichts gebucht
    gruen   belegt (mindestens eine bestaetigte Buchung)
    rot     hier war eine Buchung, die storniert wurde - der Tag ist wieder frei
    amber   zu einer Buchung an diesem Tag gab es eine Umbuchung

Stornierte Buchungen bleiben in der Datenbank stehen (``status='cancelled'``).
Sie zaehlen nie als belegt, werden aber angezeigt: Wer auf den Tag klickt, soll
sehen, dass da einmal etwas war.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.reports.occupancy import UNASSIGNED

#: Zustand eines Tages, absteigend nach Gewicht (der erste zutreffende gewinnt).
BELEGT = "belegt"
STORNIERT = "storniert"
FREI = "frei"


@dataclass
class DayBooking:
    """Eine Buchung, wie sie an einem bestimmten Tag erscheint."""

    booking_id: int
    booking_reference: str
    guest_name: str
    unit_id: int | None
    unit_name: str
    arrival_date: date
    departure_date: date
    status: str
    #: Der Gast reist an diesem Tag an bzw. ab.
    arrival: bool = False
    departure: bool = False
    #: Zu dieser Buchung gibt es mindestens eine Umbuchung.
    changed: bool = False

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"

    @property
    def occupies(self) -> bool:
        """Belegt diese Buchung die Nacht auf diesen Tag?

        Der reine Abreisetag zaehlt nicht - dort kann schon der naechste Gast
        anreisen. Eine Buchung mit gleichem An- und Abreisetag zaehlt dagegen.
        """
        return self.arrival or not self.departure


@dataclass
class CalendarDay:
    day: date
    #: Gehoert der Tag zum angezeigten Monat? Randtage fuellen nur das Raster.
    in_month: bool = True
    bookings: list[DayBooking] = field(default_factory=list)

    @property
    def active(self) -> list[DayBooking]:
        """Alle nicht stornierten Eintraege - auch reine Abreisen."""
        return [booking for booking in self.bookings if not booking.cancelled]

    @property
    def occupied(self) -> list[DayBooking]:
        """Die Eintraege, die den Tag wirklich belegen."""
        return [booking for booking in self.active if booking.occupies]

    @property
    def cancelled(self) -> list[DayBooking]:
        return [booking for booking in self.bookings if booking.cancelled]

    @property
    def status(self) -> str:
        if self.occupied:
            return BELEGT
        # Rot nur, wenn die stornierte Buchung den Tag belegt haette.
        return STORNIERT if any(b.occupies for b in self.cancelled) else FREI

    @property
    def changed(self) -> bool:
        """Amber-Markierung: zu einer Buchung des Tages gab es eine Umbuchung."""
        return any(booking.changed for booking in self.bookings)


@dataclass
class MonthCalendar:
    year: int
    month: int
    first_day: date
    last_day: date
    #: Wochen von Montag bis Sonntag, inklusive Randtagen der Nachbarmonate.
    weeks: list[list[CalendarDay]]

    @property
    def days(self) -> list[CalendarDay]:
        return [day for week in self.weeks for day in week if day.in_month]

    @property
    def occupied_days(self) -> int:
        return sum(1 for day in self.days if day.status == BELEGT)


def month_range(year: int, month: int) -> tuple[date, date]:
    """Erster und letzter Tag des Monats."""
    if not 1 <= month <= 12:
        raise ValueError("Monat muss zwischen 1 und 12 liegen.")
    last = _calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def grid_range(first_day: date, last_day: date) -> tuple[date, date]:
    """Raster von Montag vor dem Monatsanfang bis Sonntag nach dem Monatsende."""
    start = first_day - timedelta(days=first_day.weekday())
    end = last_day + timedelta(days=6 - last_day.weekday())
    return start, end


def build_month(session: Session, *, year: int, month: int) -> MonthCalendar:
    """Baut den Monat aus den Buchungen der Datenbank - inklusive Randtagen."""
    first_day, last_day = month_range(year, month)
    start, end = grid_range(first_day, last_day)

    bookings = repo.bookings_in_period(session, start=start, end=end, include_cancelled=True)
    changed_ids = repo.booking_ids_with_changes(session, [b.id for b in bookings])

    days: dict[date, CalendarDay] = {}
    day = start
    while day <= end:
        days[day] = CalendarDay(day=day, in_month=first_day <= day <= last_day)
        day += timedelta(days=1)

    for booking in bookings:
        # Belegt sind die Naechte: Anreisetag bis einschliesslich Vortag der Abreise.
        # Der Abreisetag selbst wird nur als Abreise markiert, nicht als belegt.
        span_start = max(booking.arrival_date, start)
        span_end = min(booking.departure_date, end)
        current = span_start
        while current <= span_end:
            cell = days.get(current)
            if cell is not None and (current < booking.departure_date or booking.arrival_date == booking.departure_date):
                cell.bookings.append(
                    _entry(booking, current, changed=booking.id in changed_ids)
                )
            current += timedelta(days=1)
        # Abreisetag: nur vermerken, wenn er im Raster liegt und kein belegter Tag ist.
        leaving = days.get(booking.departure_date)
        if leaving is not None and booking.departure_date != booking.arrival_date:
            leaving.bookings.append(
                _entry(booking, booking.departure_date, changed=booking.id in changed_ids)
            )

    ordered = [days[key] for key in sorted(days)]
    weeks = [ordered[index : index + 7] for index in range(0, len(ordered), 7)]
    return MonthCalendar(
        year=year, month=month, first_day=first_day, last_day=last_day, weeks=weeks
    )


def _entry(booking, day: date, *, changed: bool) -> DayBooking:
    return DayBooking(
        booking_id=booking.id,
        booking_reference=booking.booking_reference,
        guest_name=booking.guest_name,
        unit_id=booking.unit_id,
        unit_name=booking.unit.name if booking.unit else UNASSIGNED,
        arrival_date=booking.arrival_date,
        departure_date=booking.departure_date,
        status=booking.status,
        arrival=day == booking.arrival_date,
        departure=day == booking.departure_date,
        changed=changed,
    )
