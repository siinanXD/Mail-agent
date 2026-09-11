"""Datenzugriff auf Mandantendaten. Enthaelt bewusst keine Agent- oder LLM-Logik.

Die Mandantentrennung ist doppelt abgesichert:

* jede Abfrage filtert hier ausdruecklich auf ``tenant_id``,
* PostgreSQL erzwingt dasselbe zusaetzlich per Row-Level-Security.

Die erste Sicherung haelt die Tests auf SQLite ehrlich (dort gibt es kein RLS),
die zweite faengt ab, was die erste irgendwann vergisst. Neue Zeilen bekommen
den Mandanten der Session (``app.tenancy.tenant_id_for``).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.database.models import (
    Booking,
    BookingChange,
    Cancellation,
    CleaningDispatch,
    CleaningSchedule,
    Email,
    EmailEmbedding,
    StaffMember,
    StaffUnit,
    Unit,
)
from app.tenancy import tenant_id_for
from app.units import display_name, normalize_unit_name


def _tenant(session: Session) -> int:
    return tenant_id_for(session)


# Werte aus Mails haben keine Laengengrenze, die Spalten schon. Postgres lehnt
# zu lange Werte ab (SQLite ignoriert die Laenge - in den Tests fiel es nie auf):
# Eine Mail an viele Empfaenger scheiterte so bei jedem Versuch und ging nach
# den Wiederholungen des Watchers endgueltig verloren.


def _length(column) -> int:
    return column.expression.type.length


def _fit(value: str | None, length: int) -> str | None:
    """Anzeigetext auf die Spaltenlaenge kuerzen."""
    return value if value is None or len(value) <= length else value[:length]


def _fit_key(value: str, length: int) -> str:
    """Schluessel kuerzen, ohne dass zwei verschiedene gleich werden.

    Reines Abschneiden machte aus zwei langen IDs mit gleichem Anfang dieselbe -
    der Hash des ganzen Werts haelt sie auseinander und bleibt stabil.
    """
    if len(value) <= length:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"{value[: length - len(digest) - 1]}#{digest}"


# ---------------------------------------------------------------- E-Mails


def email_exists(session: Session, provider_message_id: str) -> bool:
    """Dublettenpruefung vor der teuren LLM-Extraktion."""
    stored_id = _fit_key(provider_message_id, _length(Email.provider_message_id))
    return (
        session.scalar(
            select(Email.id).where(
                Email.tenant_id == _tenant(session),
                Email.provider_message_id == stored_id,
            )
        )
        is not None
    )


def known_message_ids(session: Session, candidates: list[str]) -> set[str]:
    """Alle bereits importierten IDs aus einer Kandidatenliste - so wie uebergeben.

    Gesucht wird nach der gespeicherten (ggf. gekuerzten) Form, sonst erkaennte
    die Dublettenpruefung lange IDs nie wieder.
    """
    if not candidates:
        return set()
    length = _length(Email.provider_message_id)
    by_stored_id = {_fit_key(candidate, length): candidate for candidate in candidates}
    rows = session.scalars(
        select(Email.provider_message_id).where(
            Email.tenant_id == _tenant(session),
            Email.provider_message_id.in_(list(by_stored_id)),
        )
    )
    return {by_stored_id[row] for row in rows}


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
    provider_message_id = _fit_key(provider_message_id, _length(Email.provider_message_id))
    email = session.scalar(
        select(Email).where(
            Email.tenant_id == tenant_id,
            Email.provider_message_id == provider_message_id,
        )
    )
    if email is None:
        email = Email(tenant_id=tenant_id, provider_message_id=provider_message_id)
        session.add(email)

    email.sender = _fit(sender, _length(Email.sender))
    email.recipient = _fit(recipient, _length(Email.recipient))
    email.subject = _fit(subject, _length(Email.subject))
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


def search_emails(session: Session, *, limit: int = 20, **filters) -> list[Email]:
    """Mails zu den Filtern von ``_email_query``, neueste zuerst, hoechstens ``limit``."""
    stmt = _email_query(session, **filters).order_by(Email.received_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def count_emails(session: Session, **filters) -> int:
    """Anzahl aller Mails zu denselben Filtern wie ``search_emails`` - ohne limit."""
    stmt = _email_query(session, **filters)
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def _email_query(
    session: Session,
    *,
    subject: str | None = None,
    sender: str | None = None,
    text: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    booking_reference: str | None = None,
    email_type: str | None = None,
):
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
        # Zu einer Buchungsnummer gehoeren Buchungs-, Umbuchungs- und Stornierungsmails.
        from_bookings = select(Booking.source_email_id).where(
            Booking.tenant_id == tenant_id,
            _same_reference(booking_reference),
        )
        from_cancellations = (
            select(Cancellation.source_email_id)
            .join(Booking, Cancellation.booking_id == Booking.id)
            .where(
                Cancellation.tenant_id == tenant_id,
                _same_reference(booking_reference),
            )
        )
        # Umbuchungen haengen nicht an Booking.source_email_id, sondern ueber
        # booking_changes an der Buchung.
        from_changes = (
            select(BookingChange.source_email_id)
            .join(Booking, BookingChange.booking_id == Booking.id)
            .where(
                BookingChange.tenant_id == tenant_id,
                _same_reference(booking_reference),
            )
        )
        stmt = stmt.where(
            or_(
                Email.id.in_(from_bookings),
                Email.id.in_(from_cancellations),
                Email.id.in_(from_changes),
            )
        )

    return stmt


# ---------------------------------------------------------------- Objekte


def get_or_create_unit(session: Session, raw_name: str) -> Unit | None:
    """Findet das Objekt anhand des normalisierten Namens oder legt es an.

    Objekte sind je Mandant getrennt: "Ferienwohnung Seeblick" bei zwei
    Mandanten sind zwei verschiedene Objekte.
    """
    key = _fit_key(normalize_unit_name(raw_name or ""), _length(Unit.normalized_name))
    if not key:
        return None

    tenant_id = _tenant(session)
    unit = session.scalar(
        select(Unit).where(Unit.tenant_id == tenant_id, Unit.normalized_name == key)
    )
    if unit is None:
        unit = Unit(
            tenant_id=tenant_id,
            name=_fit(display_name(raw_name), _length(Unit.name)),
            normalized_name=key,
        )
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
    observed_at: datetime | None = None,
) -> Booking:
    """Legt eine Buchung an oder aktualisiert sie.

    ``observed_at`` ist der Eingang der Mail, aus der die Angaben stammen. Die
    Aktualitaet wird je Angabe geprueft: Kennt die Buchung fuer ein Feld schon
    einen neueren Stand (etwa aus einer Umbuchung), fuellt die aeltere Mail dort
    nur noch eine Luecke - Felder ohne neueren Stand uebernimmt sie trotzdem.
    """
    tenant_id = _tenant(session)
    booking_reference = _fit_key(booking_reference, _length(Booking.booking_reference))
    booking = session.scalar(
        select(Booking).where(
            Booking.tenant_id == tenant_id,
            Booking.booking_reference == booking_reference,
        )
    )
    snapshot: datetime | None = None
    as_of: dict[str, datetime | None] = {}
    if booking is None:
        booking = Booking(tenant_id=tenant_id, booking_reference=booking_reference)
        session.add(booking)
    else:
        # Vor dem Ersetzen der Quellmail bestimmen - sie fliesst in den Stand ein.
        snapshot = _snapshot_as_of(booking)
        as_of = {name: booking_field_as_of(session, booking, name) for name in BOOKING_FIELDS}

    def may_set(name: str) -> bool:
        known = as_of.get(name)
        return observed_at is None or known is None or observed_at >= known

    if source_email_id is not None:
        # Der Aufrufer entscheidet, welche Mail die Quelle ist. Trifft die
        # Buchungsmail nach der Stornomail ein, ersetzt sie den Platzhalter.
        booking.source_email_id = source_email_id

    # Bei einer neuen Buchung ist booking.guest_name noch None - die Spalte ist
    # aber NOT NULL, deshalb der leere String als Fallback.
    if may_set("guest_name") or not booking.guest_name:
        booking.guest_name = _fit(guest_name or booking.guest_name or "", _length(Booking.guest_name))
    if arrival_date and (may_set("arrival_date") or booking.arrival_date is None):
        booking.arrival_date = arrival_date
    if departure_date and (may_set("departure_date") or booking.departure_date is None):
        booking.departure_date = departure_date
    if unit_id is not None and (may_set("unit") or booking.unit_id is None):
        booking.unit_id = unit_id
    booking.status = status

    # Den Stand der neuesten vollstaendigen Mail festschreiben - er war evtl. nur
    # aus der Quellmail abgeleitet, und die wurde eben ggf. durch eine aeltere ersetzt.
    known_snapshots = [value for value in (snapshot, observed_at) if value is not None]
    if known_snapshots:
        booking.state_as_of = max(known_snapshots)
    session.flush()
    return booking


#: Angaben, deren Aktualitaet einzeln verfolgt wird - Namen wie in BookingChange.field.
BOOKING_FIELDS = ("guest_name", "arrival_date", "departure_date", "unit")


def _snapshot_as_of(booking: Booking) -> datetime | None:
    """Eingang der neuesten Mail, die die Buchung als Ganzes beschrieben hat.

    Buchungen von vor Migration 0005 haben keinen gespeicherten Wert - dann die Quellmail.
    """
    if booking.state_as_of is not None:
        return booking.state_as_of
    return booking.source_email.received_at if booking.source_email is not None else None


def booking_field_as_of(session: Session, booking: Booking, field: str) -> datetime | None:
    """Wie aktuell ist eine einzelne Angabe der Buchung?

    Die neuere von: letzter vollstaendiger Mail und letzter Umbuchung genau
    dieses Felds. Eine Umbuchung der Abreise macht so nicht auch das Objekt
    "neuer" - eine aeltere Objekt-Umbuchung darf danach noch greifen.
    """
    candidates = [_snapshot_as_of(booking)]
    if booking.id is not None:
        candidates.append(
            session.scalar(
                select(func.max(BookingChange.changed_at)).where(
                    BookingChange.tenant_id == booking.tenant_id,
                    BookingChange.booking_id == booking.id,
                    BookingChange.field == field,
                )
            )
        )
    return max((value for value in candidates if value is not None), default=None)


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
    stmt = _booking_query(
        session,
        guest_name=guest_name,
        start_date=start_date,
        end_date=end_date,
        status=status,
        booking_reference=booking_reference,
        unit_name=unit_name,
    )
    stmt = stmt.order_by(Booking.arrival_date).limit(limit)
    return list(session.scalars(stmt))


def count_bookings(
    session: Session,
    *,
    guest_name: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    status: str | None = None,
    booking_reference: str | None = None,
    unit_name: str | None = None,
) -> int:
    """Anzahl aller Buchungen zu denselben Filtern wie ``search_bookings`` - ohne limit."""
    stmt = _booking_query(
        session,
        guest_name=guest_name,
        start_date=start_date,
        end_date=end_date,
        status=status,
        booking_reference=booking_reference,
        unit_name=unit_name,
    )
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def _booking_query(
    session: Session,
    *,
    guest_name: str | None,
    start_date: date | None,
    end_date: date | None,
    status: str | None,
    booking_reference: str | None,
    unit_name: str | None,
):
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
    return stmt


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
    """Exakt, nur ohne Gross-/Kleinschreibung.

    Kein LIKE: "_" und "%" in einer Buchungsnummer waeren sonst Platzhalter -
    und eine Stornomail fuer "BK_123" wuerde die fremde Buchung "BK-123" stornieren.
    """
    return session.scalar(
        select(Booking).where(
            Booking.tenant_id == _tenant(session),
            _same_reference(reference),
        )
    )


def _same_reference(reference: str):
    stored = _fit_key(reference.strip(), _length(Booking.booking_reference))
    return func.lower(Booking.booking_reference) == stored.lower()


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
    guest_name: str | None = None,
    booking_reference: str | None = None,
) -> int:
    """Anzahl zu denselben Filtern wie ``search_cancellations`` - ohne limit."""
    stmt = _cancellation_query(
        tenant_id=_tenant(session),
        start_date=start_date,
        end_date=end_date,
        guest_name=guest_name,
        booking_reference=booking_reference,
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
    overwrite: bool = True,
) -> BookingChange:
    """Protokolliert eine Umbuchung, idempotent pro Mail und Feld.

    ``overwrite=False`` laesst einen vorhandenen Eintrag unveraendert - fuer
    veraltete Umbuchungen, deren urspruenglich protokolliertes "vorher" beim
    erneuten Einlesen nicht verloren gehen soll.
    """
    tenant_id = _tenant(session)
    old_value = _fit(old_value, _length(BookingChange.old_value))
    new_value = _fit(new_value, _length(BookingChange.new_value))
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
            if overwrite:
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


def _is_real_change():
    """Eintraege mit vorher == nachher sind nur Zeitstempel fuer die Aktualitaet.

    Sie entstehen, wenn eine Umbuchung den aktuellen Wert bestaetigt, und
    tauchen weder im Verlauf noch bei der Suche als Umbuchung auf.
    """
    return BookingChange.old_value.is_distinct_from(BookingChange.new_value)


def search_booking_changes(
    session: Session, *, limit: int = 50, **filters
) -> list[BookingChange]:
    """Umbuchungen zu den Filtern von ``_booking_change_query``, neueste zuerst."""
    stmt = _booking_change_query(session, **filters)
    return list(
        session.scalars(stmt.order_by(BookingChange.changed_at.desc()).limit(limit))
    )


def count_booking_changes(session: Session, **filters) -> int:
    """Anzahl zu denselben Filtern wie ``search_booking_changes`` - ohne limit."""
    stmt = _booking_change_query(session, **filters)
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def _booking_change_query(
    session: Session,
    *,
    booking_reference: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
):
    stmt = select(BookingChange).where(
        BookingChange.tenant_id == _tenant(session), _is_real_change()
    )
    if booking_reference:
        stmt = stmt.join(Booking, BookingChange.booking_id == Booking.id).where(
            Booking.booking_reference.ilike(f"%{booking_reference}%")
        )
    if start_date:
        stmt = stmt.where(BookingChange.changed_at >= _start_of_day(start_date))
    if end_date:
        stmt = stmt.where(BookingChange.changed_at <= _end_of_day(end_date))
    return stmt


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
                    _is_real_change(),
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


# ---------------------------------------------------------------- Mitarbeiter


class UnknownUnitError(ValueError):
    """Die Wohnung gibt es nicht - oder sie gehoert einem anderen Mandanten."""


def list_staff(session: Session, *, active_only: bool = False) -> list[StaffMember]:
    stmt = select(StaffMember).where(StaffMember.tenant_id == _tenant(session))
    if active_only:
        stmt = stmt.where(StaffMember.active.is_(True))
    return list(session.scalars(stmt.order_by(func.lower(StaffMember.name), StaffMember.id)))


def get_staff(session: Session, staff_id: int) -> StaffMember | None:
    # Kein session.get(): das wuerde die Mandantenpruefung umgehen.
    return session.scalar(
        select(StaffMember).where(
            StaffMember.id == staff_id, StaffMember.tenant_id == _tenant(session)
        )
    )


def create_staff(
    session: Session, *, name: str, phone: str, unit_ids: list[int], active: bool = True
) -> StaffMember:
    member = StaffMember(
        tenant_id=_tenant(session),
        name=_fit(name, _length(StaffMember.name)),
        phone=phone,
        active=active,
    )
    session.add(member)
    _assign_units(session, member, unit_ids)
    session.flush()
    return member


def update_staff(
    session: Session,
    member: StaffMember,
    *,
    name: str,
    phone: str,
    unit_ids: list[int],
    active: bool,
) -> StaffMember:
    member.name = _fit(name, _length(StaffMember.name))
    member.phone = phone
    member.active = active
    _assign_units(session, member, unit_ids)
    session.flush()
    return member


def delete_staff(session: Session, member: StaffMember) -> None:
    session.delete(member)
    session.flush()


def _assign_units(session: Session, member: StaffMember, unit_ids: list[int]) -> None:
    """Setzt die Wohnungen eines Mitarbeiters - nur Wohnungen des eigenen Mandanten."""
    tenant_id = _tenant(session)
    wanted = set(unit_ids)
    found = (
        set(session.scalars(select(Unit.id).where(Unit.tenant_id == tenant_id, Unit.id.in_(wanted))))
        if wanted
        else set()
    )
    if wanted - found:
        raise UnknownUnitError(
            "Unbekannte Wohnung: " + ", ".join(str(unit_id) for unit_id in sorted(wanted - found))
        )

    # Bestehende Zuordnungen bleiben stehen. Alle neu anzulegen verletzte
    # (staff_id, unit_id) unique, weil die neuen Zeilen vor dem Loeschen der alten kommen.
    member.assignments = [a for a in member.assignments if a.unit_id in wanted]
    existing = {assignment.unit_id for assignment in member.assignments}
    for unit_id in sorted(wanted - existing):
        member.assignments.append(StaffUnit(tenant_id=tenant_id, unit_id=unit_id))


# ---------------------------------------------------------------- Putzplan-Versand


def get_cleaning_schedule(session: Session) -> CleaningSchedule | None:
    return session.scalar(
        select(CleaningSchedule).where(CleaningSchedule.tenant_id == _tenant(session))
    )


def save_cleaning_schedule(
    session: Session, *, enabled: bool, weekday: int, send_time: time, now: datetime
) -> CleaningSchedule:
    """Speichert den Versandtermin.

    Wird eingeschaltet oder der Termin verschoben, gilt er ab ``now`` - Termine
    davor holt der Versand nicht nach.
    """
    schedule = get_cleaning_schedule(session)
    if schedule is None:
        schedule = CleaningSchedule(
            tenant_id=_tenant(session), enabled=False, send_weekday=weekday, send_time=send_time
        )
        session.add(schedule)

    rescheduled = (
        (enabled and not schedule.enabled)
        or schedule.send_weekday != weekday
        or schedule.send_time != send_time
    )
    if rescheduled or schedule.active_since is None:
        schedule.active_since = now
    schedule.enabled = enabled
    schedule.send_weekday = weekday
    schedule.send_time = send_time
    session.flush()
    return schedule


def add_dispatch(
    session: Session,
    *,
    staff_id: int,
    week_start: date,
    kind: str,
    success: bool,
    tasks: list[dict],
    created_at: datetime,
    provider_message_id: str | None = None,
    error: str | None = None,
) -> CleaningDispatch:
    dispatch = CleaningDispatch(
        tenant_id=_tenant(session),
        staff_id=staff_id,
        week_start=week_start,
        kind=kind,
        success=success,
        tasks=tasks,
        created_at=created_at,
        provider_message_id=_fit(provider_message_id, _length(CleaningDispatch.provider_message_id)),
        error=error,
    )
    session.add(dispatch)
    session.flush()
    return dispatch


def dispatches_for_week(session: Session, week_start: date) -> list[CleaningDispatch]:
    """Alle Nachrichten zu einer Woche, aelteste zuerst."""
    return list(
        session.scalars(
            select(CleaningDispatch)
            .where(
                CleaningDispatch.tenant_id == _tenant(session),
                CleaningDispatch.week_start == week_start,
            )
            .order_by(CleaningDispatch.created_at, CleaningDispatch.id)
        )
    )


def dispatched_weeks(session: Session, *, since: date) -> list[date]:
    """Wochen ab ``since``, fuer die schon etwas zugestellt wurde."""
    return list(
        session.scalars(
            select(CleaningDispatch.week_start)
            .where(
                CleaningDispatch.tenant_id == _tenant(session),
                CleaningDispatch.success.is_(True),
                CleaningDispatch.week_start >= since,
            )
            .distinct()
            .order_by(CleaningDispatch.week_start)
        )
    )


# ---------------------------------------------------------------- Wohnungsprofile


#: Profilfelder, die von Hand gepflegt werden. ``access`` ist nicht dabei -
#: Zugangsdaten gehen verschluesselt ueber ``access_encrypted``.
UNIT_PROFILE_FIELDS = (
    "description",
    "house_rules",
    "rooms",
    "beds",
    "size_sqm",
    "max_guests",
    "cleaning_window",
    "address",
    "floor",
)


def get_unit(session: Session, unit_id: int) -> Unit | None:
    # Kein session.get(): das wuerde die Mandantenpruefung umgehen.
    return session.scalar(
        select(Unit).where(Unit.id == unit_id, Unit.tenant_id == _tenant(session))
    )


def update_unit_profile(
    session: Session, unit: Unit, *, access_encrypted: str | None = None, **fields
) -> Unit:
    """Setzt die Profilfelder. Nicht uebergebene Felder bleiben unveraendert.

    ``access_encrypted`` kommt fertig verschluesselt aus der API-Schicht -
    dieses Modul kennt keine Schluessel.
    """
    for name in UNIT_PROFILE_FIELDS:
        if name not in fields:
            continue
        value = fields[name]
        if isinstance(value, str):
            value = value.strip() or None
            # Beschreibung und Hausregeln sind Text ohne Laengengrenze - dort
            # gibt es nichts zu kuerzen, und _fit koennte mit None nicht rechnen.
            laenge = _length(getattr(Unit, name))
            if value and laenge:
                value = _fit(value, laenge)
        setattr(unit, name, value)
    unit.access_encrypted = access_encrypted
    session.flush()
    return unit


def staff_by_unit(session: Session) -> dict[int, list[StaffMember]]:
    """Welche aktiven Mitarbeiter fuer welche Wohnung zustaendig sind."""
    rows = session.execute(
        select(StaffUnit.unit_id, StaffMember)
        .join(StaffMember, StaffUnit.staff_id == StaffMember.id)
        .where(
            StaffUnit.tenant_id == _tenant(session),
            StaffMember.active.is_(True),
        )
        .order_by(func.lower(StaffMember.name))
    )
    by_unit: dict[int, list[StaffMember]] = {}
    for unit_id, member in rows:
        by_unit.setdefault(unit_id, []).append(member)
    return by_unit


# ---------------------------------------------------------------- Kalender


def booking_ids_with_changes(session: Session, booking_ids: list[int]) -> set[int]:
    """Welche dieser Buchungen tatsaechlich umgebucht wurden (vorher != nachher)."""
    if not booking_ids:
        return set()
    return set(
        session.scalars(
            select(BookingChange.booking_id)
            .where(
                BookingChange.tenant_id == _tenant(session),
                BookingChange.booking_id.in_(booking_ids),
                _is_real_change(),
            )
            .distinct()
        )
    )


def get_booking(session: Session, booking_id: int) -> Booking | None:
    return session.scalar(
        select(Booking).where(
            Booking.id == booking_id, Booking.tenant_id == _tenant(session)
        )
    )


def changes_for_booking(session: Session, booking_id: int) -> list[BookingChange]:
    """Umbuchungen einer Buchung, aelteste zuerst."""
    return list(
        session.scalars(
            select(BookingChange)
            .where(
                BookingChange.tenant_id == _tenant(session),
                BookingChange.booking_id == booking_id,
                _is_real_change(),
            )
            .order_by(BookingChange.changed_at, BookingChange.id)
        )
    )


def cancellation_for_booking(session: Session, booking_id: int) -> Cancellation | None:
    return session.scalar(
        select(Cancellation)
        .where(
            Cancellation.tenant_id == _tenant(session),
            Cancellation.booking_id == booking_id,
        )
        .order_by(Cancellation.cancelled_at.desc())
    )


# ---------------------------------------------------------------- Helfer


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time())


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, datetime.max.time())
