"""Reinigungen je Mitarbeiter, die WhatsApp-Nachricht dazu und der Versandtermin.

Eine Reinigung ist faellig an jedem Abreisetag einer nicht stornierten Buchung -
genau wie im Excel-Putzplan (``app.reports.cleaning_plan``). Reist am selben Tag
der naechste Gast an, ist es ein Wechsel: Dann muss die Wohnung bis zur Anreise
fertig sein.

Der Plan wird immer erst beim Versand aus der Datenbank berechnet. Stornierungen
und Umbuchungen, die bis dahin eingegangen sind, stecken also schon drin.

In die Nachricht kommen bewusst keine Gastnamen: Die Reinigungskraft braucht Tag
und Wohnung, und die Nachricht laeuft ueber Twilio und WhatsApp.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.reports.occupancy import UNASSIGNED

WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

#: WhatsApp begrenzt die Laenge von Vorlagenvariablen - darueber wird gekuerzt.
MAX_VARIABLE_LENGTH = 900

TURNOVER_NOTE = "Wechsel: neuer Gast reist am selben Tag an"


@dataclass(frozen=True)
class CleaningTask:
    day: date
    unit_id: int | None
    unit_name: str
    #: Am selben Tag reist der naechste Gast an.
    turnover: bool = False

    @property
    def key(self) -> tuple[date, int | None]:
        return (self.day, self.unit_id)

    def to_json(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "unit_id": self.unit_id,
            "unit": self.unit_name,
            "turnover": self.turnover,
        }

    @classmethod
    def from_json(cls, data: dict) -> CleaningTask:
        return cls(
            day=date.fromisoformat(data["day"]),
            unit_id=data.get("unit_id"),
            unit_name=data.get("unit") or "",
            turnover=bool(data.get("turnover")),
        )


def _order(task: CleaningTask) -> tuple[date, str]:
    return (task.day, task.unit_name.lower())


def cleaning_tasks(session: Session, *, start: date, end: date) -> list[CleaningTask]:
    """Alle faelligen Reinigungen von ``start`` bis ``end`` (einschliesslich)."""
    bookings = repo.bookings_in_period(session, start=start, end=end)
    arrivals = {(booking.unit_id, booking.arrival_date) for booking in bookings}

    tasks: dict[tuple[date, int | None], CleaningTask] = {}
    for booking in bookings:
        day = booking.departure_date
        if not start <= day <= end:
            continue
        tasks[(day, booking.unit_id)] = CleaningTask(
            day=day,
            unit_id=booking.unit_id,
            unit_name=booking.unit.name if booking.unit else UNASSIGNED,
            turnover=(booking.unit_id, day) in arrivals,
        )
    return sorted(tasks.values(), key=_order)


@dataclass
class StaffPlan:
    staff_id: int
    name: str
    phone: str
    tasks: list[CleaningTask]


@dataclass
class WeekPlan:
    week_start: date
    staff: list[StaffPlan]
    #: Reinigungen, fuer die kein aktiver Mitarbeiter zustaendig ist - auch solche
    #: aus Buchungen ohne erkannte Wohnung.
    unassigned: list[CleaningTask]

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=6)


def week_plan(session: Session, week_start: date) -> WeekPlan:
    """Reinigungen der Woche, verteilt auf die aktiven Mitarbeiter.

    Putzen zwei Mitarbeiter dieselbe Wohnung, steht die Reinigung bei beiden.
    """
    tasks = cleaning_tasks(session, start=week_start, end=week_start + timedelta(days=6))
    covered: set[int] = set()
    plans = []
    for member in repo.list_staff(session, active_only=True):
        unit_ids = {assignment.unit_id for assignment in member.assignments}
        covered |= unit_ids
        plans.append(
            StaffPlan(
                staff_id=member.id,
                name=member.name,
                phone=member.phone,
                tasks=[task for task in tasks if task.unit_id in unit_ids],
            )
        )
    unassigned = [task for task in tasks if task.unit_id not in covered]
    return WeekPlan(week_start=week_start, staff=plans, unassigned=unassigned)


# ---------------------------------------------------------------- Aenderungen


@dataclass
class TaskDiff:
    added: list[CleaningTask] = field(default_factory=list)
    removed: list[CleaningTask] = field(default_factory=list)
    #: Gleicher Tag, gleiche Wohnung - aber der Wechsel kam hinzu oder fiel weg.
    changed: list[CleaningTask] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_tasks(old: list[CleaningTask], new: list[CleaningTask]) -> TaskDiff:
    before = {task.key: task for task in old}
    after = {task.key: task for task in new}
    return TaskDiff(
        added=sorted((t for key, t in after.items() if key not in before), key=_order),
        removed=sorted((t for key, t in before.items() if key not in after), key=_order),
        changed=sorted(
            (t for key, t in after.items() if key in before and before[key].turnover != t.turnover),
            key=_order,
        ),
    )


# ---------------------------------------------------------------- Wochen und Termine


def week_start_of(day: date) -> date:
    """Montag der Woche, in der ``day`` liegt."""
    return day - timedelta(days=day.weekday())


def upcoming_week_start(day: date) -> date:
    """Montag der naechsten beginnenden Woche - an einem Montag der Tag selbst."""
    return day + timedelta(days=(7 - day.weekday()) % 7)


def week_label(week_start: date) -> str:
    end = week_start + timedelta(days=6)
    return f"KW {week_start.isocalendar().week} ({week_start:%d.%m.}–{end:%d.%m.%Y})"


def latest_slot(now: datetime, weekday: int, send_time: time) -> datetime:
    """Der letzte Versandtermin, der schon erreicht ist."""
    slot = datetime.combine(now.date() - timedelta(days=(now.weekday() - weekday) % 7), send_time)
    return slot if slot <= now else slot - timedelta(days=7)


def next_slot(now: datetime, weekday: int, send_time: time) -> datetime:
    return latest_slot(now, weekday, send_time) + timedelta(days=7)


def due_week(
    now: datetime, *, weekday: int, send_time: time, active_since: datetime | None
) -> date | None:
    """Welche Woche jetzt verschickt werden muss - oder None.

    Verschickt wird zum letzten erreichten Termin die kommende Woche (Montag bis
    Sonntag): Sonntag 18:00 schickt die Woche ab dem naechsten Tag, Montag 07:00
    die Woche ab heute. Verpasst ist ein Termin erst, wenn seine Woche vorbei ist -
    lief der Server Sonntagabend nicht, geht der Plan beim naechsten Start raus.
    Termine vor dem Einschalten zaehlen nicht (``active_since``).
    """
    if active_since is None:
        return None
    slot = latest_slot(now, weekday, send_time)
    if slot < active_since:
        return None
    week_start = upcoming_week_start(slot.date())
    if now.date() > week_start + timedelta(days=6):
        return None
    return week_start


# ---------------------------------------------------------------- Nachrichten


def task_line(task: CleaningTask) -> str:
    line = f"{WEEKDAYS[task.day.weekday()]} {task.day:%d.%m.} – {task.unit_name}"
    return f"{line} ({TURNOVER_NOTE})" if task.turnover else line


def _count(number: int) -> str:
    return "1 Reinigung" if number == 1 else f"{number} Reinigungen"


def _single_line(value: str) -> str:
    """Vorlagenvariablen: keine Zeilenumbrueche, begrenzte Laenge."""
    flat = re.sub(r"\s+", " ", value).strip()
    return flat if len(flat) <= MAX_VARIABLE_LENGTH else flat[: MAX_VARIABLE_LENGTH - 1] + "…"


def _variables(*values: str) -> dict[str, str]:
    return {str(index): _single_line(value) for index, value in enumerate(values, start=1)}


def weekly_message(
    *, tenant_name: str, staff_name: str, week_start: date, tasks: list[CleaningTask]
) -> tuple[str, dict[str, str]]:
    """Wochenplan als freier Text und als Vorlagenvariablen (Name, Titel, Plan)."""
    title = f"Putzplan {week_label(week_start)} von {tenant_name}"
    lines = [task_line(task) for task in tasks]
    if lines:
        body = [f"Hallo {staff_name},", "", f"dein {title} – {_count(len(lines))}:", "", *lines]
    else:
        body = [f"Hallo {staff_name},", "", f"dein {title}: Es stehen keine Reinigungen für dich an."]
    return "\n".join(body), _variables(
        staff_name, title, "; ".join(lines) or "keine Reinigungen"
    )


def update_message(
    *,
    tenant_name: str,
    staff_name: str,
    week_start: date,
    diff: TaskDiff,
    tasks: list[CleaningTask],
) -> tuple[str, dict[str, str]]:
    """Aenderung nach dem Versand: was neu ist, was entfaellt, und der ganze aktuelle Plan."""
    title = f"Putzplan {week_label(week_start)} von {tenant_name}"
    changes = (
        [f"Neu: {task_line(task)}" for task in diff.added]
        + [f"Entfällt: {task_line(task)}" for task in diff.removed]
        + [
            f"Geändert: {task_line(task)}" if task.turnover
            else f"Geändert: {task_line(task)} (kein Wechsel mehr)"
            for task in diff.changed
        ]
    )
    current = [task_line(task) for task in tasks] or ["keine Reinigungen"]
    body = [
        f"Hallo {staff_name},",
        "",
        f"Änderung am {title}:",
        "",
        *changes,
        "",
        "Dein aktueller Plan:",
        *current,
    ]
    return "\n".join(body), _variables(
        staff_name,
        f"Änderung am {title}",
        f"{'; '.join(changes)}. Aktuell: {'; '.join(current)}",
    )
