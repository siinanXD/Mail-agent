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

    Je Objekt: stays (alle Aufenthalte im Zeitraum, auch vorher begonnene),
    arrivals, departures, turnover_days (Ab- und Anreise am selben Tag),
    cleaning_days (Reinigung faellig) und free_nights. Eine Nacht ist belegt vom
    Anreisetag bis vor dem Abreisetag: Fuer "Anreise 12., Abreise 15." muessen
    die Naechte 12., 13. und 14. frei sein. Stornierte Buchungen zaehlen nicht.
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

    return to_json(
        {
            "from": start_date,
            "to": end,
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


def _stay(stay: Stay) -> dict:
    return {
        "guest": stay.guest,
        "booking_reference": stay.booking_reference,
        "arrival": stay.arrival,
        "departure": stay.departure,
    }
