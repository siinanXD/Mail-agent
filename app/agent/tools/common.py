"""Gemeinsame Helfer fuer die Agent-Tools."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from app.database.models import Booking, Cancellation, Email


#: Obergrenze fuer Listen eines Tools. "limit" waehlt das LLM selbst - ohne
#: Deckel koennte es tausende Zeilen anfordern und den Kontext (und die
#: Rechnung) sprengen.
MAX_RESULTS = 100


def clamp_limit(limit: int, maximum: int = MAX_RESULTS) -> int:
    return max(1, min(int(limit), maximum))


def to_json(payload: Any) -> str:
    """Kompakte, LLM-freundliche Serialisierung des Tool-Ergebnisses."""
    return json.dumps(payload, ensure_ascii=False, default=_default)


def _default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def email_summary(email: Email) -> dict[str, Any]:
    return {
        "email_id": email.id,
        "subject": email.subject,
        "sender": email.sender,
        "received_at": email.received_at,
        "email_type": email.email_type,
        "preview": email.body[:200],
    }


def booking_summary(booking: Booking) -> dict[str, Any]:
    return {
        "booking_id": booking.id,
        "booking_reference": booking.booking_reference,
        "guest_name": booking.guest_name,
        "arrival_date": booking.arrival_date,
        "departure_date": booking.departure_date,
        "status": booking.status,
        "unit": booking.unit.name if booking.unit else None,
        "source_email_id": booking.source_email_id,
    }


def cancellation_summary(cancellation: Cancellation) -> dict[str, Any]:
    booking = cancellation.booking
    return {
        "cancellation_id": cancellation.id,
        "booking_reference": booking.booking_reference if booking else None,
        "guest_name": booking.guest_name if booking else None,
        "cancelled_at": cancellation.cancelled_at,
        "reason": cancellation.reason,
        "source_email_id": cancellation.source_email_id,
    }
