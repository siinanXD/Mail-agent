"""Verlauf und E-Mail-Detailansicht mit Belegen - je Mandant."""

from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.auth import CurrentUser, require_user
from app.database import repositories as repo
from app.evidence import collect_evidence, merge_matches
from app.tenancy import tenant_session
from app.units import normalize_unit_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["verlauf"])


class TimelineEntry(BaseModel):
    email_id: int
    received_at: datetime
    email_type: str
    subject: str
    sender: str
    guest_name: str | None = None
    unit: str | None = None
    unit_source: str | None = None
    booking_reference: str | None = None
    summary: str = ""


class TimelineResponse(BaseModel):
    count: int
    counts_by_type: dict[str, int]
    entries: list[TimelineEntry]


class EvidenceItem(BaseModel):
    field: str
    label: str
    value: str
    found: bool


class Highlight(BaseModel):
    start: int
    end: int


class EmailDetail(BaseModel):
    email_id: int
    provider_message_id: str
    subject: str
    sender: str
    recipient: str
    received_at: datetime
    email_type: str
    body: str
    evidence: list[EvidenceItem]
    highlights: list[Highlight]
    booking_reference: str | None = None
    unit: str | None = None


@router.get("/timeline", response_model=TimelineResponse)
def timeline(
    types: list[str] | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: CurrentUser = Depends(require_user),
) -> TimelineResponse:
    """Alle E-Mails des Mandanten chronologisch, angereichert um die Daten."""
    with tenant_session(user.tenant_id) as session:
        emails = repo.timeline_emails(
            session, email_types=types, search=search, limit=limit
        )
        units = repo.list_units(session)

        entries = [
            _to_entry(email, repo.records_for_email(session, email.id), units)
            for email in emails
        ]
        return TimelineResponse(
            count=len(entries),
            counts_by_type=repo.type_counts(session),
            entries=entries,
        )


@router.get("/emails/{email_id}", response_model=EmailDetail)
def email_detail(email_id: int, user: CurrentUser = Depends(require_user)) -> EmailDetail:
    """Die Original-Mail plus Beleg, welcher Wert wo im Text steht."""
    with tenant_session(user.tenant_id) as session:
        email = repo.get_email(session, email_id)
        if email is None:
            # Auch bei fremden Mails 404 statt 403 - sonst verraet die Antwort,
            # dass es die ID bei einem anderen Mandanten gibt.
            raise HTTPException(status_code=404, detail="E-Mail nicht gefunden")

        records = repo.records_for_email(session, email.id)
        booking = records["booking"]
        cancellation = records["cancellation"]
        changes = records["changes"]

        evidence = collect_evidence(
            subject=email.subject,
            body=email.body,
            booking=booking,
            cancellation=cancellation,
            changes=changes,
        )

        # Fundstellen beziehen sich auf "Betreff\nBody" - fuer die Anzeige im
        # Body-Feld muss der Betreff-Versatz abgezogen werden.
        offset = len(email.subject) + 1
        raw = [m for item in evidence for m in item.matches if m.start >= offset]
        highlights = [
            Highlight(start=m.start - offset, end=m.end - offset)
            for m in merge_matches(raw)
        ]

        linked = booking or (cancellation.booking if cancellation else None)
        return EmailDetail(
            email_id=email.id,
            provider_message_id=email.provider_message_id,
            subject=email.subject,
            sender=email.sender,
            recipient=email.recipient,
            received_at=email.received_at,
            email_type=email.email_type,
            body=email.body,
            evidence=[
                EvidenceItem(
                    field=item.field, label=item.label, value=item.value, found=item.found
                )
                for item in evidence
            ],
            highlights=highlights,
            booking_reference=linked.booking_reference if linked else None,
            unit=linked.unit.name if linked and linked.unit else None,
        )


def _to_entry(email, records: dict, units: list) -> TimelineEntry:
    booking = records["booking"]
    cancellation = records["cancellation"]
    changes = records["changes"]
    linked = booking or (cancellation.booking if cancellation else None)
    if linked is None and changes:
        linked = changes[0].booking

    unit_name = linked.unit.name if linked and linked.unit else None
    unit_source = "verknuepft" if unit_name else None
    if unit_name is None:
        unit_name = _unit_from_text(email, units)
        unit_source = "aus dem Text erkannt" if unit_name else None

    return TimelineEntry(
        email_id=email.id,
        received_at=email.received_at,
        email_type=email.email_type,
        subject=email.subject,
        sender=email.sender,
        guest_name=linked.guest_name if linked else None,
        unit=unit_name,
        unit_source=unit_source,
        booking_reference=linked.booking_reference if linked else None,
        summary=_summary(email, booking, cancellation, changes),
    )


def _unit_from_text(email, units: list) -> str | None:
    """Objekt aus dem Mailtext raten, wenn kein Datensatz daran haengt."""
    haystack = normalize_unit_name(f"{email.subject} {email.body}")
    for unit in units:
        if unit.normalized_name and unit.normalized_name in haystack:
            return unit.name
    return None


def _summary(email, booking, cancellation, changes) -> str:
    if booking is not None:
        return _stay(booking.arrival_date, booking.departure_date)
    if cancellation is not None:
        return f"Grund: {cancellation.reason}" if cancellation.reason else "Ohne Angabe von Gruenden"
    if changes:
        labels = {"arrival_date": "Anreise", "departure_date": "Abreise", "unit": "Objekt"}
        return " · ".join(
            f"{labels.get(c.field, c.field)}: {c.old_value or '—'} → {c.new_value}"
            for c in changes
        )
    return email.subject


def _stay(arrival: date | None, departure: date | None) -> str:
    if arrival and departure:
        return f"{arrival.strftime('%d.%m.')} – {departure.strftime('%d.%m.%Y')}"
    if arrival:
        return f"ab {arrival.strftime('%d.%m.%Y')}"
    return ""
