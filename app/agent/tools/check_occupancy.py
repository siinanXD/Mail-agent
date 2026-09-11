"""Tool: Belegung, Anreisen, Abreisen und Reinigungen fuer einen Zeitraum."""

from __future__ import annotations

from datetime import date

from langchain_core.tools import tool

from app.agent.tools.common import to_json
from app.reports.occupancy import Stay, occupancy
from app.tenancy import tenant_session


@tool
def check_occupancy(
    start_date: date, end_date: date | None = None, unit_name: str | None = None
) -> str:
    """Belegung je Objekt fuer einen Zeitraum - erzeugt KEINE Datei.

    Fuer Fragen wie "Wer reist am Samstag ab?", "Wer wohnt gerade in der FeWo
    Seeblick?", "Ist Haus Anna vom 12. bis 15. frei?" oder "Was muss diese Woche
    geputzt werden?". Ohne end_date gilt nur der eine Tag; hoechstens 62 Tage.
    unit_name filtert auf ein Objekt und toleriert Schreibvarianten.

    Oben stehen die Uebersichten ueber alle Objekte: cleanings_total und
    cleanings (faellige Reinigungen, eine je Abreisetag und Objekt), arrivals
    und departures. Darunter je Objekt: stays (alle Aufenthalte im Zeitraum, auch
    vorher begonnene), turnover_days (Ab- und Anreise am selben Tag) und
    free_nights. free_nights sind unbelegte Naechte - KEINE Reinigungen.

    Eine Nacht ist belegt vom Anreisetag bis vor dem Abreisetag: Fuer "Anreise
    12., Abreise 15." muessen die Naechte 12., 13. und 14. frei sein. Stornierte
    Buchungen zaehlen nicht.
    """
    end = end_date or start_date
    try:
        with tenant_session() as session:
            units = occupancy(session, start=start_date, end=end, unit_name=unit_name)
    except ValueError as error:
        return to_json({"error": str(error)})

    if unit_name and not units:
        return to_json(
            {"error": f"Kein Objekt passt zu {unit_name!r}. list_units zeigt alle Objekte."}
        )

    # Flache Uebersichten zuerst: Aus den verschachtelten Listen je Objekt las das
    # LLM im Test Abreisen nicht vollstaendig und hielt freie Naechte fuer Reinigungen.
    cleanings = sorted(
        {(day, unit.unit) for unit in units for day in unit.cleaning_days}
    )
    return to_json(
        {
            "from": start_date,
            "to": end,
            "hinweis": (
                "cleanings sind die faelligen Reinigungen (je Abreisetag und Objekt). "
                "free_nights sind unbelegte Naechte, keine Reinigungen."
            ),
            "cleanings_total": len(cleanings),
            "cleanings": [{"date": day, "unit": name} for day, name in cleanings],
            "arrivals": _flat(units, "arrivals", "arrival"),
            "departures": _flat(units, "departures", "departure"),
            "units": [
                {
                    "unit": unit.unit,
                    "stays": [_stay(stay) for stay in unit.stays],
                    "arrivals": [_stay(stay) for stay in unit.arrivals],
                    "departures": [_stay(stay) for stay in unit.departures],
                    "turnover_days": unit.turnover_days,
                    "cleaning_days": unit.cleaning_days,
                    "free_nights": unit.free_nights,
                }
                for unit in units
            ],
        }
    )


def _flat(units, attribute: str, date_field: str) -> list[dict]:
    """An- bzw. Abreisen aller Objekte in einer Liste, nach Datum und Objekt."""
    entries = [
        {
            "date": getattr(stay, date_field),
            "unit": unit.unit,
            "guest": stay.guest,
            "booking_reference": stay.booking_reference,
        }
        for unit in units
        for stay in getattr(unit, attribute)
    ]
    return sorted(entries, key=lambda entry: (entry["date"], entry["unit"]))


def _stay(stay: Stay) -> dict:
    return {
        "guest": stay.guest,
        "booking_reference": stay.booking_reference,
        "arrival": stay.arrival,
        "departure": stay.departure,
    }
