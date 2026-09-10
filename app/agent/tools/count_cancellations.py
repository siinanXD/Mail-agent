"""Tool: exakte Anzahl Stornierungen (SQL COUNT)."""

from __future__ import annotations

from datetime import date

from langchain_core.tools import tool

from app.agent.tools.common import to_json
from app.database import repositories as repo
from app.tenancy import tenant_session


@tool
def count_cancellations(
    start_date: date | None = None, end_date: date | None = None
) -> str:
    """Liefert die exakte Anzahl der Stornierungen in einem Zeitraum.

    Immer dieses Tool verwenden, wenn nach einer Anzahl gefragt wird - niemals
    Ergebnisse aus einer semantischen Suche zaehlen.
    """
    with tenant_session() as session:
        count = repo.count_cancellations(
            session, start_date=start_date, end_date=end_date
        )
        return to_json(
            {"count": count, "start_date": start_date, "end_date": end_date}
        )
