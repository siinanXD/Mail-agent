"""Datenzugriff auf Mandantendaten. Enthaelt bewusst keine Agent- oder LLM-Logik.

Die Mandantentrennung ist doppelt abgesichert:

* jede Abfrage filtert hier ausdruecklich auf ``tenant_id``,
* PostgreSQL erzwingt dasselbe zusaetzlich per Row-Level-Security.

Die erste Sicherung haelt die Tests auf SQLite ehrlich (dort gibt es kein RLS),
die zweite faengt ab, was die erste irgendwann vergisst. Neue Zeilen bekommen
den Mandanten der Session (``app.tenancy.tenant_id_for``).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.database.models import (
    Booking,
    BookingChange,
    Cancellation,
    Email,
    EmailEmbedding,
    Unit,
)
from app.tenancy import tenant_id_for
from app.units import display_name, normalize_unit_name


def _tenant(session: Session) -> int:
    return tenant_id_for(session)


# ---------------------------------------------------------------- E-Mails


def email_exists(session: Session, provider_message_id: str) -> bool:
    """Dublettenpruefung vor der teuren LLM-Extraktion."""
    return (
        session.scalar(
            select(Email.id).where(
                Email.tenant_id == _tenant(session),
                Email.provider_message_id == provider_message_id,
            )
        )
        is not None
    )


def known_message_ids(session: Session, candidates: list[str]) -> set[str]:
    """Alle bereits importierten IDs aus einer Kandidatenliste."""
    if not candidates:
        return set()
    rows = session.scalars(
        select(Email.provider_message_id).where(
            Email.tenant_id == _tenant(session),
            Email.provider_message_id.in_(candidates),
        )
    )
    return set(rows)


def upsert_email(
    session: Session,
    *,
    provider_message_id: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    received_at: datetime,
    email_type: str,
) -> Email:
    """Legt eine E-Mail an oder aktualisiert sie anhand der provider_message_id."""
    tenant_id = _tenant(session)
    email = session.scalar(
        select(Email).where(
            Email.tenant_id == tenant_id,
            Email.provider_message_id == provider_message_id,
        )
    )
    if email is None:
        email = Email(tenant_id=tenant_id, provider_message_id=provider_message_id)
        session.add(email)

    email.sender = sender
    email.recipient = recipient
    email.subject = subject
    email.body = body
    email.received_at = received_at
    email.email_type = email_type
    session.flush()
    return email


def get_email(session: Session, email_id: int) -> Email | None:
    # Kein session.get(): das wuerde die Mandantenpruefung umgehen.
    return session.scalar(
        select(Email).where(Email.id == email_id, Email.tenant_id == _tenant(session))
    )


def search_emails(
    session: Session,
    *,
    subject: str | None = None,
    sender: str | None = None,
    text: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    booking_reference: str | None = None,
    email_type: str | None = None,
    limit: int = 20,
) -> list[Email]:
    tenant_id = _tenant(session)
    stmt = select(Email).where(Email.tenant_id == tenant_id)

    if subject:
        stmt = stmt.where(Email.subject.ilike(f"%{subject}%"))
    if sender:
        stmt = stmt.where(Email.sender.ilike(f"%{sender}%"))
    if text:
        stmt = stmt.where(
            or_(Email.body.ilike(f"%{text}%"), Email.subject.ilike(f"%{text}%"))
        )
    if email_type:
        stmt = stmt.where(Email.email_type == email_type)
    if start_date:
        stmt = stmt.where(Email.received_at >= _start_of_day(start_date))
    if end_date:
        stmt = stmt.where(Email.received_at <= _end_of_day(end_date))
    if booking_reference:
        # Zu einer Buchungsnummer gehoeren Buchungs- UND Stornierungsmails.
        from_bookings = select(Booking.source_email_id).where(
            Booking.tenant_id == tenant_id,
            Booking.booking_reference.ilike(booking_reference),
        )
        from_cancellations = (
            select(Cancellation.source_email_id)
            .join(Booking, Cancellation.booking_id == Booking.id)
            .where(
                Cancellation.tenant_id == tenant_id,
                Booking.booking_reference.ilike(booking_reference),
            )
        )
        stmt = stmt.where(
            or_(Email.id.in_(from_bookings), Email.id.in_(from_cancellations))
        )

    stmt = stmt.order_by(Email.received_at.desc()).limit(limit)
    return list(session.scalars(stmt))


# ---------------------------------------------------------------- Objekte


def get_or_create_unit(session: Session, raw_name: str) -> Unit | None:
    """Findet das Objekt anhand des normalisierten Namens oder legt es an.

    Objekte sind je Mandant getrennt: "Ferienwohnung Seeblick" bei zwei
    Mandanten sind zwei verschiedene Objekte.
    """
    key = normalize_unit_name(raw_name or "")
    if not key:
        return None

    tenant_id = _tenant(session)
    unit = session.scalar(
        select(Unit).where(Unit.tenant_id == tenant_id, Unit.normalized_name == key)
    )
    if unit is None:
        unit = Unit(tenant_id=tenant_id, name=display_name(raw_name), normalized_name=key)
        session.add(unit)
        session.flush()
    return unit


def list_units(session: Session) -> list[Unit]:
    return list(
        session.scalars(
            select(Unit).where(Unit.tenant_id == _tenant(session)).order_by(Unit.name)
        )
    )


# ---------------------------------------------------------------- Buchungen


def upsert_booking(
    session: Session,
    *,
    booking_reference: str,
    guest_name: str,
    arrival_date: date | None,
    departure_date: date | None,
    status: str = "confirmed",
    unit_id: int | None = None,
    source_email_id: int | None = None,
) -> Booking:
    tenant_id = _tenant(session)
    booking = session.scalar(
        select(Booking).where(
            Booking.tenant_id == tenant_id,
            Booking.booking_reference == booking_reference,
        )
    )
    if booking is None:
        booking = Booking(tenant_id=tenant_id, booking_reference=booking_reference)
        session.add(booking)

    if source_email_id is not None:
        # Der Aufrufer entscheidet, welche Mail die Quelle ist. Trifft die
        # Buchungsmail nach der Stornomail ein, ersetzt sie den Platzhalter.
        booking.source_email_id = source_email_id

    # Bei einer neuen Buchung ist booking.guest_name noch None - die Spalte ist
    # aber NOT NULL, deshalb der leere String als Fallback.
    booking.guest_name = guest_name or booking.guest_name or ""
    if arrival_date:
        booking.arrival_date = arrival_date
    if departure_date:
        booking.departure_date = departure_date
    if unit_id is not None:
        booking.unit_id = unit_id
    booking.status = status
    session.flush()
    return booking


def search_bookings(
    session: Session,
    *,
    guest_name: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    status: str | None = None,
    booking_reference: str | None = None,
    unit_name: str | None = None,
    limit: int = 50,
) -> list[Booking]:
    """Zeitraumfilter bezieht sich auf das Anreisedatum."""
    stmt = select(Booking).where(Booking.tenant_id == _tenant(session))

    if guest_name:
        stmt = stmt.where(Booking.guest_name.ilike(f"%{guest_name}%"))
    if unit_name:
        stmt = stmt.join(Unit, Booking.unit_id == Unit.id).where(
            Unit.normalized_name.ilike(f"%{normalize_unit_name(unit_name)}%")
        )
    if booking_reference:
        stmt = stmt.where(Booking.booking_reference.ilike(f"%{booking_reference}%"))
    if status:
        stmt = stmt.where(Booking.status == status)
    if start_date:
        stmt = stmt.where(Booking.arrival_date >= start_date)
    if end_date:
        stmt = stmt.where(Booking.arrival_date <= end_date)

    stmt = stmt.order_by(Booking.arrival_date).limit(limit)
    return list(session.scalars(stmt))


def find_bookings_by_guest_exact(
    session: Session,
    *,
    guest_name: str,
    arrival_date: date | None = None,
    status: str | None = None,
    limit: int = 10,
) -> list[Booking]:
    """Exakter Namensabgleich (case-insensitiv) - fuer zustandsaendernde Pfade.

    ``search_bookings`` sucht bewusst unscharf; das ist fuer die Recherche
    richtig, aber zu riskant, um damit eine Buchung zu stornieren.
    """
    stmt = select(Booking).where(
        Booking.tenant_id == _tenant(session),
        func.lower(func.trim(Booking.guest_name)) == guest_name.strip().lower(),
    )
    if arrival_date:
        stmt = stmt.where(Booking.arrival_date == arrival_date)
    if status:
        stmt = stmt.where(Booking.status == status)
    return list(session.scalars(stmt.limit(limit)))


def get_booking_by_reference(session: Session, reference: str) -> Booking | None:
    return session.scalar(
        select(Booking).where(
            Booking.tenant_id == _tenant(session),
            Booking.booking_reference.ilike(reference),
        )
    )


# ---------------------------------------------------------------- Stornierungen


def add_cancellation(
    session: Session,
    *,
    booking_id: int | None,
    cancelled_at: datetime,
    reason: str | None,
    source_email_id: int | None,
) -> Cancellation:
    """Idempotent pro Quell-E-Mail."""
    tenant_id = _tenant(session)
    existing = None
    if source_email_id is not None:
        existing = session.scalar(
            select(Cancellation).where(
                Cancellation.tenant_id == tenant_id,
                Cancellation.source_email_id == source_email_id,
            )
        )
    cancellation = existing or Cancellation(
        tenant_id=tenant_id, source_email_id=source_email_id
    )
    if existing is None:
        session.add(cancellation)

    cancellation.booking_id = booking_id
    cancellation.cancelled_at = cancelled_at
    cancellation.reason = reason
    session.flush()
    return cancellation


def get_cancellation_by_source_email(
    session: Session, email_id: int
) -> Cancellation | None:
    return session.scalar(
        select(Cancellation).where(
            Cancellation.tenant_id == _tenant(session),
            Cancellation.source_email_id == email_id,
        )
    )


def _cancellation_query(
    *,
    tenant_id: int,
    start_date: date | None,
    end_date: date | None,
    guest_name: str | None,
    booking_reference: str | None,
):
    stmt = select(Cancellation).where(Cancellation.tenant_id == tenant_id)
    if start_date:
        stmt = stmt.where(Cancellation.cancelled_at >= _start_of_day(start_date))
    if end_date:
        stmt = stmt.where(Cancellation.cancelled_at <= _end_of_day(end_date))
    if guest_name or booking_reference:
        stmt = stmt.join(Booking, Cancellation.booking_id == Booking.id)
        if guest_name:
            stmt = stmt.where(Booking.guest_name.ilike(f"%{guest_name}%"))
        if booking_reference:
            stmt = stmt.where(Booking.booking_reference.ilike(f"%{booking_reference}%"))
    return stmt


def search_cancellations(
    session: Session,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    guest_name: str | None = None,
    booking_reference: str | None = None,
    limit: int = 50,
) -> list[Cancellation]:
    stmt = _cancellation_query(
        tenant_id=_tenant(session),
        start_date=start_date,
        end_date=end_date,
        guest_name=guest_name,
        booking_reference=booking_reference,
    )
    stmt = stmt.order_by(Cancellation.cancelled_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def count_cancellations(
    session: Session,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    stmt = _cancellation_query(
        tenant_id=_tenant(session),
        start_date=start_date,
        end_date=end_date,
        guest_name=None,
        booking_reference=None,
    )
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


# ---------------------------------------------------------------- Aenderungen


def add_booking_change(
    session: Session,
    *,
    booking_id: int,
    changed_at: datetime,
    field: str,
    old_value: str | None,
    new_value: str | None,
    source_email_id: int | None,
) -> BookingChange:
    """Protokolliert eine Umbuchung, idempotent pro Mail und Feld."""
    tenant_id = _tenant(session)
    if source_email_id is not None:
        existing = session.scalar(
            select(BookingChange).where(
                BookingChange.tenant_id == tenant_id,
                BookingChange.source_email_id == source_email_id,
                BookingChange.booking_id == booking_id,
                BookingChange.field == field,
            )
        )
        if existing is not None:
            existing.old_value = old_value
            existing.new_value = new_value
            session.flush()
            return existing

    change = BookingChange(
        tenant_id=tenant_id,
        booking_id=booking_id,
        changed_at=changed_at,
        field=field,
        old_value=old_value,
        new_value=new_value,
        source_email_id=source_email_id,
    )
    session.add(change)
    session.flush()
    return change


def search_booking_changes(
    session: Session,
    *,
    booking_reference: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
) -> list[BookingChange]:
    stmt = select(BookingChange).where(BookingChange.tenant_id == _tenant(session))
    if booking_reference:
        stmt = stmt.join(Booking, BookingChange.booking_id == Booking.id).where(
            Booking.booking_reference.ilike(f"%{booking_reference}%")
        )
    if start_date:
        stmt = stmt.where(BookingChange.changed_at >= _start_of_day(start_date))
    if end_date:
        stmt = stmt.where(BookingChange.changed_at <= _end_of_day(end_date))
    return list(
        session.scalars(stmt.order_by(BookingChange.changed_at.desc()).limit(limit))
    )


# ---------------------------------------------------------------- Belegung


def bookings_in_period(
    session: Session, *, start: date, end: date, include_cancelled: bool = False
) -> list[Booking]:
    """Alle Buchungen, die sich mit [start, end] ueberschneiden.

    Grundlage fuer den Putzplan: eine Buchung zaehlt zur Woche, wenn ihr
    Aufenthalt den Zeitraum beruehrt - nicht nur wenn sie darin beginnt.
    """
    stmt = (
        select(Booking)
        .where(
            Booking.tenant_id == _tenant(session),
            Booking.arrival_date.is_not(None),
            Booking.departure_date.is_not(None),
            Booking.arrival_date <= end,
            Booking.departure_date >= start,
        )
        .order_by(Booking.arrival_date)
    )
    if not include_cancelled:
        stmt = stmt.where(Booking.status != "cancelled")
    return list(session.scalars(stmt))


# ---------------------------------------------------------------- Verlauf


def timeline_emails(
    session: Session,
    *,
    email_types: list[str] | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[Email]:
    """E-Mails fuer den Verlauf, neueste zuerst."""
    stmt = select(Email).where(Email.tenant_id == _tenant(session))
    if email_types:
        stmt = stmt.where(Email.email_type.in_(email_types))
    if search:
        stmt = stmt.where(
            or_(Email.subject.ilike(f"%{search}%"), Email.body.ilike(f"%{search}%"))
        )
    stmt = stmt.order_by(Email.received_at.desc(), Email.id.desc()).limit(limit)
    return list(session.scalars(stmt))


def records_for_email(session: Session, email_id: int) -> dict[str, object]:
    """Alle strukturierten Daten, die aus dieser E-Mail entstanden sind.

    ``bookings`` ist eine Liste: Eine Beds24-Gruppenbuchung legt aus einer Mail
    je Zimmer eine eigene Buchung an.
    """
    tenant_id = _tenant(session)
    return {
        "bookings": list(
            session.scalars(
                select(Booking)
                .where(Booking.tenant_id == tenant_id, Booking.source_email_id == email_id)
                .order_by(Booking.id)
            )
        ),
        "cancellation": session.scalar(
            select(Cancellation).where(
                Cancellation.tenant_id == tenant_id,
                Cancellation.source_email_id == email_id,
            )
        ),
        "changes": list(
            session.scalars(
                select(BookingChange).where(
                    BookingChange.tenant_id == tenant_id,
                    BookingChange.source_email_id == email_id,
                )
            )
        ),
    }


def type_counts(session: Session) -> dict[str, int]:
    """Anzahl E-Mails je Typ - fuer die Kennzahl-Kacheln."""
    rows = session.execute(
        select(Email.email_type, func.count())
        .where(Email.tenant_id == _tenant(session))
        .group_by(Email.email_type)
    )
    return {email_type: count for email_type, count in rows}


# ---------------------------------------------------------------- Embeddings


def replace_embeddings(
    session: Session,
    *,
    email_id: int,
    chunks: list[str],
    vectors: list[list[float]],
    metadata: dict,
) -> int:
    tenant_id = _tenant(session)
    session.execute(
        delete(EmailEmbedding).where(
            EmailEmbedding.tenant_id == tenant_id,
            EmailEmbedding.email_id == email_id,
        )
    )
    for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
        session.add(
            EmailEmbedding(
                tenant_id=tenant_id,
                email_id=email_id,
                chunk_index=index,
                chunk=chunk,
                embedding=vector,
                meta=metadata,
            )
        )
    session.flush()
    return len(chunks)


def search_embeddings(
    session: Session, *, query_vector: list[float], limit: int = 5
) -> list[tuple[EmailEmbedding, float]]:
    """Semantische Suche via pgvector (Cosine-Distanz, kleiner = aehnlicher)."""
    distance = EmailEmbedding.embedding.cosine_distance(query_vector).label("distance")
    stmt = (
        select(EmailEmbedding, distance)
        .where(EmailEmbedding.tenant_id == _tenant(session))
        .order_by(distance)
        .limit(limit)
    )
    return [(row[0], float(row[1])) for row in session.execute(stmt)]


# ---------------------------------------------------------------- Helfer


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time())


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, datetime.max.time())
