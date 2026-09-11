"""Belegung je Objekt fuer einen Zeitraum - fuer den Assistenten, ohne Datei.

Beantwortet, was der Putzplan als Excel zeigt, direkt als Daten: wer wohnt wann
wo, wer reist an oder ab, wo ist ein Wechseltag, wann muss geputzt werden und
welche Naechte sind frei.

Eine Nacht ist belegt vom Anreisetag bis vor dem Abreisetag. Am Abreisetag kann
also ein neuer Gast anreisen. Stornierte Buchungen und Buchungen ohne An- oder
Abreisedatum zaehlen nicht - wie im Putzplan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.units import normalize_unit_name

#: Laengster Zeitraum je Anfrage - die Antwort geht komplett in den LLM-Kontext.
MAX_DAYS = 62

#: Wie im Putzplan: Buchungen ohne erkanntes Objekt.
UNASSIGNED = "Nicht zugeordnet"


@dataclass
class Stay:
    guest: str
    booking_reference: str
    arrival: date
    departure: date


@dataclass
class UnitOccupancy:
    unit: str
    #: Alle Aufenthalte, die den Zeitraum beruehren - auch schon vorher begonnene.
    stays: list[Stay]
    arrivals: list[Stay]
    departures: list[Stay]
    #: Abreise und Anreise am selben Tag.
    turnover_days: list[date]
    #: Reinigung faellig: jeder Abreisetag im Zeitraum (wie im Putzplan).
    cleaning_days: list[date]
    free_nights: list[date]


def occupancy(
    session: Session, *, start: date, end: date, unit_name: str | None = None
) -> list[UnitOccupancy]:
    """Belegung je Objekt fuer jeden Tag von ``start`` bis ``end`` (einschliesslich)."""
    if end < start:
        raise ValueError("Das Ende des Zeitraums liegt vor dem Beginn.")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"Bitte hoechstens {MAX_DAYS} Tage auf einmal abfragen.")

    stays_by_unit: dict[str, list[Stay]] = {}
    for booking in repo.bookings_in_period(session, start=start, end=end):
        name = booking.unit.name if booking.unit else UNASSIGNED
        stays_by_unit.setdefault(name, []).append(
            Stay(
                guest=booking.guest_name,
                booking_reference=booking.booking_reference,
                arrival=booking.arrival_date,
                departure=booking.departure_date,
            )
        )
    # Objekte ohne Aufenthalt gehoeren dazu: "komplett frei" ist auch eine Antwort.
    for unit in repo.list_units(session):
        stays_by_unit.setdefault(unit.name, [])

    if unit_name:
        wanted = normalize_unit_name(unit_name)
        stays_by_unit = {
            name: stays
            for name, stays in stays_by_unit.items()
            if wanted and wanted in normalize_unit_name(name)
        }

    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    return [
        _unit_occupancy(name, stays_by_unit[name], days, start, end)
        for name in sorted(stays_by_unit, key=_sort_key)
    ]


def _unit_occupancy(
    name: str, stays: list[Stay], days: list[date], start: date, end: date
) -> UnitOccupancy:
    stays = sorted(stays, key=lambda stay: (stay.arrival, stay.departure))
    arrivals = [stay for stay in stays if start <= stay.arrival <= end]
    departures = [stay for stay in stays if start <= stay.departure <= end]
    arrival_days = {stay.arrival for stay in arrivals}
    departure_days = {stay.departure for stay in departures}
    return UnitOccupancy(
        unit=name,
        stays=stays,
        arrivals=arrivals,
        departures=departures,
        turnover_days=sorted(arrival_days & departure_days),
        cleaning_days=sorted(departure_days),
        free_nights=[
            day
            for day in days
            if not any(stay.arrival <= day < stay.departure for stay in stays)
        ],
    )


def _sort_key(name: str) -> tuple[int, str]:
    return (1, name) if name == UNASSIGNED else (0, name.lower())
