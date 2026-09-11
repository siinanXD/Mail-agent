"""Tool: strukturierte Buchungssuche (SQL)."""

from __future__ import annotations

from datetime import date

from langchain_core.tools import tool

from app.agent.tools.common import booking_summary, clamp_limit, to_json
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

    "count" ist die Gesamtzahl aller passenden Buchungen. Gelistet werden
    hoechstens ``limit`` ("returned"); "truncated" zeigt, ob die Liste gekuerzt ist.
    """
    filters = dict(
        guest_name=guest_name,
        start_date=start_date,
        end_date=end_date,
        status=status,
        booking_reference=booking_reference,
        unit_name=unit_name,
    )
    with tenant_session() as session:
        bookings = repo.search_bookings(session, **filters, limit=clamp_limit(limit))
        total = repo.count_bookings(session, **filters)
        return to_json(
            {
                "count": total,
                "returned": len(bookings),
                "truncated": total > len(bookings),
                "bookings": [booking_summary(b) for b in bookings],
            }
        )
