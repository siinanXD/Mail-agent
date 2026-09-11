"""Tool: strukturierte E-Mail-Suche (SQL)."""

from __future__ import annotations

from datetime import date

from langchain_core.tools import tool

from app.agent.tools.common import clamp_limit, email_summary, to_json
from app.database import repositories as repo
from app.tenancy import tenant_session


@tool
def search_emails(
    subject: str | None = None,
    sender: str | None = None,
    text: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    booking_reference: str | None = None,
    email_type: str | None = None,
    limit: int = 20,
) -> str:
    """Sucht E-Mails ueber exakte Filter (SQL, keine semantische Suche).

    Nutze dieses Tool fuer Filter nach Betreff, Absender, Empfangsdatum,
    Buchungsnummer oder E-Mail-Typ (booking, cancellation, change, request,
    complaint, other). Fuer inhaltliche/vage Fragen stattdessen knowledge_search.

    "count" ist die Gesamtzahl aller passenden Mails. Gelistet werden hoechstens
    ``limit`` ("returned"); "truncated" zeigt, ob die Liste gekuerzt ist.
    """
    filters = dict(
        subject=subject,
        sender=sender,
        text=text,
        start_date=start_date,
        end_date=end_date,
        booking_reference=booking_reference,
        email_type=email_type,
    )
    with tenant_session() as session:
        emails = repo.search_emails(session, **filters, limit=clamp_limit(limit))
        total = repo.count_emails(session, **filters)
        return to_json(
            {
                "count": total,
                "returned": len(emails),
                "truncated": total > len(emails),
                "emails": [email_summary(e) for e in emails],
            }
        )
