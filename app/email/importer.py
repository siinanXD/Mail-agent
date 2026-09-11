"""E-Mail-Ingestion.

Ablauf pro E-Mail:
    parsen -> Typ erkennen + Daten extrahieren -> E-Mail speichern
    -> Buchung/Stornierung speichern -> chunken -> Embeddings -> pgvector
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import repositories as repo
from app.email.beds24 import parse_beds24_records
from app.email.extractor import EmailExtraction, extract
from app.email.parser import (
    ParsedEmail,
    list_email_files,
    manifest_received_at,
    parse_file,
    read_manifest,
)
from app.knowledge.indexer import index_email
from app.llm.client import LLMNotConfiguredError

logger = logging.getLogger(__name__)

#: Ein Extractor liefert ein Ergebnis - oder mehrere, wenn eine Mail mehrere
#: Buchungen enthaelt (Beds24-Gruppenbuchung, je Zimmer eine).
ExtractionResult = EmailExtraction | list[EmailExtraction]
Extractor = Callable[[ParsedEmail], ExtractionResult]


def default_extractor(parsed: ParsedEmail) -> ExtractionResult:
    """Beds24-Benachrichtigungen regelbasiert, alles andere per LLM."""
    return parse_beds24_records(parsed) or extract(parsed)


def as_records(result: ExtractionResult) -> list[EmailExtraction]:
    records = result if isinstance(result, list) else [result]
    if not records:
        raise ValueError("Extraktion ohne Ergebnis")
    return records


class ImportResult(BaseModel):
    imported: int = 0
    skipped: int = 0
    bookings: int = 0
    cancellations: int = 0
    changes: int = 0
    units: int = 0
    chunks: int = 0
    failed: list[str] = []
    #: provider_message_ids der fehlgeschlagenen Mails - fuer den Watcher, der
    #: sie beim naechsten Abruf erneut versuchen muss.
    failed_message_ids: list[str] = []


class EmailOutcome(BaseModel):
    """Was eine einzelne E-Mail bewirkt hat."""

    bookings: int = 0
    cancellations: int = 0
    changes: int = 0
    chunks: int = 0


def import_email(
    session: Session,
    parsed: ParsedEmail,
    *,
    extractor: Extractor | None = None,
    embedder=None,
) -> EmailOutcome:
    """Importiert eine einzelne E-Mail."""
    extract_fn = extractor or default_extractor
    records = as_records(extract_fn(parsed))
    data = records[0]

    email = repo.upsert_email(
        session,
        provider_message_id=parsed.provider_message_id,
        sender=parsed.sender,
        recipient=parsed.recipient,
        subject=parsed.subject,
        body=parsed.body,
        received_at=parsed.received_at,
        email_type=data.email_type,
    )

    outcome = EmailOutcome()
    for record in records:
        _apply_record(session, record, email, parsed, outcome)

    outcome.chunks = index_email(session, email, embedder=embedder)
    return outcome


def _apply_record(
    session: Session,
    data: EmailExtraction,
    email,
    parsed: ParsedEmail,
    outcome: EmailOutcome,
) -> None:
    """Schreibt Buchung, Aenderung oder Stornierung eines Extraktionsergebnisses."""
    unit = repo.get_or_create_unit(session, data.unit_name) if data.unit_name else None

    if data.email_type == "booking" and data.booking_reference:
        repo.upsert_booking(
            session,
            booking_reference=data.booking_reference,
            guest_name=data.guest_name or "",
            arrival_date=data.arrival_date,
            departure_date=data.departure_date,
            # Eine bereits stornierte Buchung darf durch das erneute Einlesen
            # der aelteren Buchungsmail nicht wieder bestaetigt werden.
            status=_booking_status(session, data.booking_reference),
            unit_id=unit.id if unit else None,
            source_email_id=email.id,
            observed_at=parsed.received_at,
        )
        outcome.bookings += 1
    elif data.email_type == "change":
        _apply_change(session, data, email, parsed, outcome)
    elif data.email_type == "cancellation":
        booking = _resolve_cancelled_booking(
            session, data, email.id, unit, observed_at=parsed.received_at
        )
        cancelled_at = (
            datetime.combine(data.cancellation_date, datetime.min.time())
            if data.cancellation_date
            else parsed.received_at
        )
        repo.add_cancellation(
            session,
            booking_id=booking.id if booking else None,
            cancelled_at=cancelled_at,
            reason=data.cancellation_reason,
            source_email_id=email.id,
        )
        outcome.cancellations += 1


def import_directory(
    session: Session,
    directory: str | Path | None = None,
    *,
    extractor: Extractor | None = None,
    embedder=None,
    reprocess: bool = False,
) -> ImportResult:
    """Importiert alle Test-E-Mails aus einem Verzeichnis (chronologisch)."""
    path = Path(directory or get_settings().sample_emails_dir)
    if not path.is_dir():
        raise FileNotFoundError(f"Verzeichnis nicht gefunden: {path}")

    result = ImportResult()
    import_emails(
        session,
        _parse_all(path, result),
        result,
        extractor=extractor,
        embedder=embedder,
        reprocess=reprocess,
    )
    return result


def import_emails(
    session: Session,
    emails: list[ParsedEmail],
    result: ImportResult | None = None,
    *,
    extractor: Extractor | None = None,
    embedder=None,
    reprocess: bool = False,
) -> ImportResult:
    """Importiert eine Liste bereits geparster E-Mails.

    Bereits bekannte ``provider_message_id``s werden uebersprungen, bevor die
    teure LLM-Extraktion laeuft - das ist die Dublettenbremse fuer den
    Dauerbetrieb am Postfach.
    """
    result = result or ImportResult()
    units_before = len(repo.list_units(session))

    known = (
        set()
        if reprocess
        else repo.known_message_ids(
            session, [mail.provider_message_id for mail in emails]
        )
    )

    for parsed in emails:
        if parsed.provider_message_id in known:
            result.skipped += 1
            continue
        try:
            # Savepoint: eine fehlerhafte Mail verwirft nur sich selbst,
            # nicht die bereits importierten Mails desselben Laufs.
            with session.begin_nested():
                outcome = import_email(
                    session, parsed, extractor=extractor, embedder=embedder
                )
            result.imported += 1
            result.bookings += outcome.bookings
            result.cancellations += outcome.cancellations
            result.changes += outcome.changes
            result.chunks += outcome.chunks
        except LLMNotConfiguredError:
            raise  # Konfigurationsfehler betrifft jede Mail - sofort abbrechen
        except Exception as error:  # eine kaputte Mail stoppt nicht den Import
            logger.exception("Import fehlgeschlagen: %s", parsed.provider_message_id)
            result.failed.append(f"{parsed.provider_message_id}: {error}")
            result.failed_message_ids.append(parsed.provider_message_id)

    result.units = len(repo.list_units(session)) - units_before
    logger.info(
        "Import: %d neu, %d uebersprungen, %d Buchungen, %d Stornierungen, "
        "%d Aenderungen, %d neue Objekte",
        result.imported,
        result.skipped,
        result.bookings,
        result.cancellations,
        result.changes,
        result.units,
    )
    return result


def _apply_change(
    session: Session,
    data: EmailExtraction,
    email,
    parsed: ParsedEmail,
    outcome: EmailOutcome,
) -> None:
    """Uebernimmt eine Umbuchung und protokolliert jede geaenderte Angabe."""
    booking = None
    if data.booking_reference:
        booking = repo.get_booking_by_reference(session, data.booking_reference)
    elif data.guest_name:
        # Wie bei Stornierungen: Nennt die Mail eine Nummer, die wir nicht
        # kennen, ist es nicht die Buchung eines gleichnamigen Gastes.
        booking = _match_booking_by_guest(session, data)

    if booking is None and data.booking_reference and data.new_arrival_date:
        # Die Mail nennt Nummer und neuen Stand (Beds24), nur die urspruengliche
        # Buchungsmail fehlt - etwa weil der Import erst nach der Buchung
        # beginnt. Dann die Buchung anlegen statt die Aenderung zu verwerfen.
        new_unit = (
            repo.get_or_create_unit(session, data.new_unit_name)
            if data.new_unit_name
            else None
        )
        repo.upsert_booking(
            session,
            booking_reference=data.booking_reference,
            guest_name=data.guest_name or "",
            arrival_date=data.new_arrival_date,
            departure_date=data.new_departure_date,
            unit_id=new_unit.id if new_unit else None,
            source_email_id=email.id,
            observed_at=parsed.received_at,
        )
        outcome.bookings += 1
        return
    if booking is None:
        logger.warning(
            "Aenderungsmail ohne zuordenbare Buchung: %s",
            parsed.provider_message_id,
        )
        return

    changed_at = parsed.received_at
    # Ist schon ein neuerer Stand bekannt (spaetere Umbuchung zuerst importiert),
    # wird diese aeltere Umbuchung nur protokolliert, nicht angewendet.
    known_state = repo.booking_state_as_of(session, booking)
    outdated = known_state is not None and changed_at < known_state
    new_unit = (
        repo.get_or_create_unit(session, data.new_unit_name)
        if data.new_unit_name
        else None
    )
    updates: list[tuple[str, object, object]] = [
        ("arrival_date", booking.arrival_date, data.new_arrival_date),
        ("departure_date", booking.departure_date, data.new_departure_date),
        (
            "unit",
            booking.unit.name if booking.unit else None,
            new_unit.name if new_unit else None,
        ),
    ]

    applied = 0
    for field, old_value, new_value in updates:
        if new_value is None or old_value == new_value:
            continue
        repo.add_booking_change(
            session,
            booking_id=booking.id,
            changed_at=changed_at,
            field=field,
            # Bei einer veralteten Umbuchung ist der aktuelle Wert nicht ihr "vorher".
            old_value=str(old_value) if old_value is not None and not outdated else None,
            new_value=str(new_value),
            source_email_id=email.id,
        )
        applied += 1
        if outdated:
            continue
        if field == "arrival_date":
            booking.arrival_date = data.new_arrival_date
        elif field == "departure_date":
            booking.departure_date = data.new_departure_date
        else:
            booking.unit_id = new_unit.id

    if outdated:
        logger.info(
            "Umbuchung %s ist aelter als der bekannte Stand - nur protokolliert",
            parsed.provider_message_id,
        )
        booking.state_as_of = known_state
    else:
        booking.state_as_of = max(changed_at, known_state) if known_state else changed_at
    session.flush()
    outcome.changes += applied


def _match_booking_by_guest(session: Session, data: EmailExtraction):
    """Ohne Buchungsnummer nur zuordnen, wenn die Buchung eindeutig ist.

    Ein Stammgast kann mehrere Buchungen haben - dann lieber gar nicht
    verknuepfen als die falsche stornieren. Die Stornierung wird trotzdem
    gespeichert und bleibt ueber die Quell-E-Mail auffindbar.
    """
    # Nennt die Mail ein Anreisedatum, muss es passen - sonst ist es eine
    # andere Buchung desselben Gastes. Beides filtert die Datenbank.
    candidates = repo.find_bookings_by_guest_exact(
        session,
        guest_name=data.guest_name,
        arrival_date=data.arrival_date,
        status="confirmed",
    )

    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        logger.warning(
            "Stornierung nicht eindeutig zuordenbar (%s, %d Kandidaten)",
            data.guest_name,
            len(candidates),
        )
    return None


def _booking_status(session: Session, reference: str) -> str:
    """"cancelled" ist im MVP ein Endzustand und wird nicht zurueckgesetzt."""
    existing = repo.get_booking_by_reference(session, reference)
    if existing is not None and existing.status == "cancelled":
        return "cancelled"
    return "confirmed"


def _parse_all(directory: Path, result: ImportResult) -> list[ParsedEmail]:
    """Parst jede Datei einzeln; defekte Dateien landen in ``failed``.

    Liegt eine ``manifest.csv`` im Verzeichnis, liefert sie das Eingangsdatum
    fuer .eml-Dateien ohne Date-Header - sonst stimmt die Reihenfolge nicht.
    """
    manifest = read_manifest(directory)
    emails: list[ParsedEmail] = []
    for file in list_email_files(directory):
        name = file.relative_to(directory).as_posix()
        try:
            emails.append(
                parse_file(file, received_at=manifest_received_at(manifest, file))
            )
        except Exception as error:
            logger.exception("Datei nicht lesbar: %s", name)
            result.failed.append(f"{name}: {error}")
    return sorted(emails, key=lambda mail: mail.received_at)


def _resolve_cancelled_booking(
    session: Session,
    data: EmailExtraction,
    email_id: int,
    unit=None,
    *,
    observed_at: datetime | None = None,
):
    """Findet die stornierte Buchung, legt sie notfalls nach."""
    # Beim erneuten Import derselben Mail die bereits getroffene Zuordnung
    # behalten - sonst wuerde die Buchung entkoppelt oder eine andere erwischt.
    existing = repo.get_cancellation_by_source_email(session, email_id)
    if existing is not None and existing.booking is not None:
        return existing.booking

    booking = None
    if data.booking_reference:
        booking = repo.get_booking_by_reference(session, data.booking_reference)
        if booking is None:
            booking = repo.upsert_booking(
                session,
                booking_reference=data.booking_reference,
                guest_name=data.guest_name or "",
                arrival_date=data.arrival_date,
                departure_date=data.departure_date,
                status="cancelled",
                unit_id=unit.id if unit else None,
                source_email_id=email_id,
                observed_at=observed_at,
            )
    elif data.guest_name:
        booking = _match_booking_by_guest(session, data)

    if booking is not None:
        booking.status = "cancelled"
        session.flush()
    return booking
