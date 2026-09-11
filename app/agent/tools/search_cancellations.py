"""Tool: strukturierte Stornierungssuche (SQL)."""

from __future__ import annotations

from datetime import date

from langchain_core.tools import tool

from app.agent.tools.common import cancellation_summary, to_json
from app.database import repositories as repo
from app.tenancy import tenant_session


@tool
def search_cancellations(
    start_date: date | None = None,
    end_date: date | None = None,
    guest_name: str | None = None,
    booking_reference: str | None = None,
    limit: int = 50,
) -> str:
    """Listet Stornierungen inklusive Grund und Quell-E-Mail.

    Zeitraumfilter beziehen sich auf das Stornierungsdatum.

    "count" ist die Gesamtzahl aller passenden Stornierungen. Gelistet werden
    hoechstens ``limit`` ("returned"); "truncated" zeigt, ob die Liste gekuerzt ist.
    """
    filters = dict(
        start_date=start_date,
        end_date=end_date,
        guest_name=guest_name,
        booking_reference=booking_reference,
    )
    with tenant_session() as session:
        cancellations = repo.search_cancellations(session, **filters, limit=limit)
        total = repo.count_cancellations(session, **filters)
        return to_json(
            {
                "count": total,
                "returned": len(cancellations),
                "truncated": total > len(cancellations),
                "cancellations": [cancellation_summary(c) for c in cancellations],
            }
        )
