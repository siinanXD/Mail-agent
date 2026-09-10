"""Tool: eine einzelne E-Mail im Volltext laden."""

from __future__ import annotations

from langchain_core.tools import tool

from app.agent.tools.common import to_json
from app.database import repositories as repo
from app.tenancy import tenant_session


@tool
def get_email(email_id: int) -> str:
    """Gibt eine einzelne E-Mail inklusive vollstaendigem Text zurueck.

    Die email_id stammt aus search_emails, knowledge_search oder aus dem Feld
    source_email_id einer Buchung/Stornierung.
    """
    with tenant_session() as session:
        email = repo.get_email(session, email_id)
        if email is None:
            return to_json({"error": f"Keine E-Mail mit id {email_id} gefunden"})
        return to_json(
            {
                "email_id": email.id,
                "subject": email.subject,
                "sender": email.sender,
                "recipient": email.recipient,
                "received_at": email.received_at,
                "email_type": email.email_type,
                "body": email.body,
            }
        )
