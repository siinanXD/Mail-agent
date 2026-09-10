"""Tool: Putzplan als Excel-Datei erzeugen."""

from __future__ import annotations

from datetime import date

from langchain_core.tools import tool

from app.agent.tools.common import to_json
from app.tenancy import tenant_session
from app.reports.cleaning_plan import export_cleaning_plan


@tool
def create_cleaning_plan(
    week: int | None = None,
    year: int | None = None,
    any_date_in_week: date | None = None,
) -> str:
    """Erstellt den Putzplan einer Kalenderwoche als Excel-Datei.

    Nur aufrufen, wenn der Nutzer ausdruecklich einen Putzplan, Reinigungsplan
    oder eine Wochenuebersicht als Datei anfordert.

    Entweder ``week`` (+ optional ``year``) angeben oder ``any_date_in_week``
    mit einem beliebigen Datum aus der gewuenschten Woche - daraus wird die
    Kalenderwoche berechnet. Der Rueckgabewert enthaelt download_path sowie eine
    Zusammenfassung der faelligen Reinigungen. download_path unveraendert
    weitergeben, ohne Domain davor.
    """
    if any_date_in_week is not None:
        iso = any_date_in_week.isocalendar()
        week, year = iso.week, iso.year
    if week is None:
        return to_json(
            {"error": "Bitte Kalenderwoche oder ein Datum aus der Woche angeben."}
        )
    year = year or date.today().year

    try:
        with tenant_session() as session:
            plan, path = export_cleaning_plan(session, year=year, week=week)
    except ValueError as error:
        return to_json({"error": f"Ungueltige Kalenderwoche: {error}"})

    return to_json(
        {
            "week": plan.week,
            "year": plan.year,
            "from": plan.start,
            "to": plan.end,
            "cleanings_total": plan.cleaning_count,
            "file": path.name,
            "download_path": f"/reports/cleaning-plan/{path.name}",
            "units": [
                {
                    "unit": row.unit_name,
                    "cleaning_days": row.cleaning_days,
                }
                for row in plan.rows
                if row.cleaning_days
            ],
        }
    )
