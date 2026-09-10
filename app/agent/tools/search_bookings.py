"""Tool: strukturierte Buchungssuche (SQL)."""

from __future__ import annotations

from datetime import date

from langchain_core.tools import tool

from app.agent.tools.common import booking_summary, to_json
from app.database import repositories as repo
from app.tenancy import tenant_session


@tool
def search_bookings(
    guest_name: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    status: str | None = None,
    booking_reference: str | None = None,
    unit_name: str | None = None,
    limit: int = 50,
) -> str:
    """Sucht Buchungen in der Datenbank.

    Zeitraumfilter (start_date/end_date) beziehen sich auf das Anreisedatum.
    status ist entweder "confirmed" oder "cancelled". unit_name filtert auf ein
    Objekt (Ferienwohnung/Haus) und toleriert Schreibvarianten wie "FeWo
    Seeblick" oder "Seeblick". Nutze dieses Tool fuer alle faktischen Fragen
    nach Buchungen, Gaesten, Objekten oder Zeitraeumen.
    """
    with tenant_session() as session:
        bookings = repo.search_bookings(
            session,
            guest_name=guest_name,
            start_date=start_date,
            end_date=end_date,
            status=status,
            booking_reference=booking_reference,
            unit_name=unit_name,
            limit=limit,
        )
        return to_json(
            {"count": len(bookings), "bookings": [booking_summary(b) for b in bookings]}
        )
