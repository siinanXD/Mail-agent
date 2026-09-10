"""Tool: semantische Suche ueber pgvector."""

from __future__ import annotations

from langchain_core.tools import tool

from app.agent.tools.common import to_json
from app.tenancy import tenant_session
from app.knowledge.retriever import semantic_search


@tool
def knowledge_search(query: str, limit: int = 5) -> str:
    """Semantische Suche im E-Mail-Text (Embeddings + pgvector).

    Fuer inhaltliche Fragen ohne exakte Filter, z.B. "Beschwerde ueber das
    Fruehstueck", "wegen Flugausfall storniert" oder "Late Check-out erwaehnt".
    Liefert Textausschnitte mit email_id - Details danach ggf. per get_email
    nachladen. Fuer Zahlen und Zeitraumfilter die SQL-Tools nutzen.
    """
    with tenant_session() as session:
        hits = semantic_search(session, query, limit=limit)
        return to_json({"count": len(hits), "results": hits})
