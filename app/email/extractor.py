"""Strukturierte Extraktion aus E-Mail-Text via LLM Structured Output."""

from __future__ import annotations

import logging
from datetime import date
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from app.email.parser import ParsedEmail
from app.llm.client import get_chat_model

logger = logging.getLogger(__name__)

EmailType = Literal[
    "booking", "cancellation", "change", "request", "complaint", "other"
]

EXTRACTION_PROMPT = """Du extrahierst strukturierte Daten aus E-Mails einer
Ferienwohnungs-Vermietung.

Regeln:
- email_type:
  "booking"      neue oder bestaetigte Buchung
  "cancellation" Stornierung
  "change"       Umbuchung: bestehende Buchung wird verschoben, verlaengert,
                 verkuerzt oder in ein anderes Objekt verlegt
  "request"      Anfrage oder Sonderwunsch
  "complaint"    Beschwerde
  "other"        alles andere
- Datumsangaben immer als ISO-Datum (YYYY-MM-DD).
- Felder, die nicht in der E-Mail stehen, bleiben null. Nichts erfinden.
- cancellation_reason ist eine kurze Zusammenfassung des genannten Grundes
  (z.B. "Flugausfall", "Krankheit"), null wenn kein Grund genannt wird.
- unit_name ist das genannte Objekt, genau so geschrieben wie in der E-Mail
  (z.B. "Ferienwohnung Seeblick", "FeWo Bergblick", "Haus Anna"). Steht kein
  Objekt in der Mail, bleibt das Feld null.
- Bei email_type "change" gehoeren die NEUEN Daten in new_arrival_date /
  new_departure_date / new_unit_name. arrival_date und departure_date sind dann
  die bisherigen Daten, falls die Mail sie nennt.

E-Mail empfangen am: {received_at}
Betreff: {subject}
Von: {sender}

{body}
"""


class EmailExtraction(BaseModel):
    """Ergebnis der Extraktion. Alle Felder sind optional ausser email_type."""

    email_type: EmailType = Field(description="Typ der E-Mail")
    booking_reference: str | None = Field(
        default=None, description="Buchungsnummer, z.B. BK-2026-001"
    )
    guest_name: str | None = Field(default=None, description="Name des Gastes")
    arrival_date: date | None = Field(default=None, description="Anreisedatum")
    departure_date: date | None = Field(default=None, description="Abreisedatum")
    cancellation_date: date | None = Field(
        default=None, description="Datum der Stornierung"
    )
    cancellation_reason: str | None = Field(
        default=None, description="Kurzer Grund der Stornierung"
    )
    unit_name: str | None = Field(
        default=None,
        description="Objekt/Ferienwohnung wie in der E-Mail genannt",
    )
    new_arrival_date: date | None = Field(
        default=None, description="Neues Anreisedatum bei einer Umbuchung"
    )
    new_departure_date: date | None = Field(
        default=None, description="Neues Abreisedatum bei einer Umbuchung"
    )
    new_unit_name: str | None = Field(
        default=None, description="Neues Objekt bei einer Umbuchung"
    )


def extract(email: ParsedEmail, model: BaseChatModel | None = None) -> EmailExtraction:
    """Extrahiert Buchungs-/Stornodaten. ``model`` erlaubt Injection in Tests."""
    chat = model or get_chat_model()
    prompt = EXTRACTION_PROMPT.format(
        received_at=email.received_at.date().isoformat(),
        subject=email.subject,
        sender=email.sender,
        body=email.body,
    )
    structured = chat.with_structured_output(EmailExtraction)
    result = structured.invoke(prompt)
    logger.debug("Extraktion fuer %s: %s", email.provider_message_id, result)
    return result
