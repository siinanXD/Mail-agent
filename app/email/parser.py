"""Parsen lokaler E-Mail-Dateien (.txt mit Headerblock, .json oder .eml)."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from email import message_from_bytes
from pathlib import Path

from pydantic import BaseModel, Field

EMAIL_SUFFIXES = {".txt", ".json", ".eml"}

#: Exporte (z.B. aus einem Postfach-Dump) legen Labels und Eingangsdatum hier ab.
MANIFEST_NAME = "manifest.csv"

_HEADER_ALIASES = {
    "message-id": "provider_message_id",
    "id": "provider_message_id",
    "from": "sender",
    "to": "recipient",
    "subject": "subject",
    "date": "received_at",
}

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
)


class ParsedEmail(BaseModel):
    """Rohdaten einer E-Mail, noch ohne semantische Interpretation."""

    provider_message_id: str
    sender: str = ""
    recipient: str = ""
    subject: str = ""
    body: str = ""
    received_at: datetime = Field(default_factory=datetime.utcnow)


class ManifestEntry(BaseModel):
    """Eine Zeile aus ``manifest.csv``."""

    intent: str = ""
    received_at: datetime | None = None


def parse_datetime(value: str) -> datetime:
    """Akzeptiert ISO-8601 sowie ein paar gaengige deutsche Schreibweisen."""
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Datum nicht interpretierbar: {value!r}")


def parse_text(content: str, *, fallback_id: str) -> ParsedEmail:
    """Parst einen einfachen Headerblock, gefolgt von einer Leerzeile und Body."""
    lines = content.replace("\r\n", "\n").split("\n")
    fields: dict[str, str] = {}
    body_start = 0

    for index, line in enumerate(lines):
        if not line.strip():
            body_start = index + 1
            break
        key, separator, value = line.partition(":")
        if not separator:
            body_start = index
            break
        mapped = _HEADER_ALIASES.get(key.strip().lower())
        if mapped:
            fields[mapped] = value.strip()
        body_start = index + 1

    body = "\n".join(lines[body_start:]).strip()
    received_at = (
        parse_datetime(fields["received_at"])
        if fields.get("received_at")
        else datetime.utcnow()
    )

    return ParsedEmail(
        provider_message_id=fields.get("provider_message_id") or fallback_id,
        sender=fields.get("sender", ""),
        recipient=fields.get("recipient", ""),
        subject=fields.get("subject", ""),
        body=body,
        received_at=received_at,
    )


def parse_json(content: str, *, fallback_id: str) -> ParsedEmail:
    data = json.loads(content)
    if isinstance(data.get("received_at"), str):
        data["received_at"] = parse_datetime(data["received_at"])
    data.setdefault("provider_message_id", fallback_id)
    return ParsedEmail(**data)


def parse_eml(
    raw: bytes, *, fallback_id: str, received_at: datetime | None = None
) -> ParsedEmail:
    """Parst eine RFC-822-Datei (MIME, quoted-printable, kodierte Betreffs).

    ``received_at`` greift nur, wenn die Datei keinen Date-Header hat - Exporte
    tragen das Eingangsdatum dann nur im Manifest.
    """
    # Import hier, weil imap_client seinerseits ParsedEmail aus diesem Modul holt.
    from app.email.imap_client import parse_message

    message = message_from_bytes(raw)
    parsed = parse_message(message)

    updates: dict[str, object] = {}
    if not message.get("Message-ID"):
        # parse_message nimmt sonst einen Hash der Rohnachricht. Der Dateiname
        # ist genauso stabil, aber im Import-Ergebnis und in Logs lesbar.
        updates["provider_message_id"] = fallback_id
    if not message.get("Date") and received_at is not None:
        updates["received_at"] = received_at
    return parsed.model_copy(update=updates)


def parse_file(path: Path, *, received_at: datetime | None = None) -> ParsedEmail:
    """``received_at`` ist der Fallback aus dem Manifest (nur fuer .eml)."""
    suffix = path.suffix.lower()
    if suffix == ".eml":
        return parse_eml(
            path.read_bytes(), fallback_id=path.stem, received_at=received_at
        )
    content = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return parse_json(content, fallback_id=path.stem)
    return parse_text(content, fallback_id=path.stem)


def list_email_files(directory: Path) -> list[Path]:
    """Alle Mail-Dateien eines Verzeichnisses inkl. Unterordnern, nach Pfad sortiert."""
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in EMAIL_SUFFIXES
    )


def read_manifest(directory: Path) -> dict[Path, ManifestEntry]:
    """Liest ``manifest.csv`` (Spalten ``file``, ``intent``, ``received_at``).

    Schluessel ist der aufgeloeste Dateipfad. Ohne Manifest: leeres Dict.
    """
    path = directory / MANIFEST_NAME
    if not path.is_file():
        return {}

    entries: dict[Path, ManifestEntry] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            file = (row.get("file") or "").strip()
            if not file:
                continue
            raw_date = (row.get("received_at") or "").strip()
            entries[(directory / file).resolve()] = ManifestEntry(
                intent=(row.get("intent") or "").strip(),
                received_at=parse_datetime(raw_date) if raw_date else None,
            )
    return entries


def manifest_received_at(
    manifest: dict[Path, ManifestEntry], path: Path
) -> datetime | None:
    entry = manifest.get(path.resolve())
    return entry.received_at if entry else None


def load_directory(directory: Path) -> list[ParsedEmail]:
    """Laedt alle E-Mails eines Verzeichnisses, chronologisch sortiert.

    Wirft bei einer kaputten Datei. Der Importer parst deshalb datei-weise,
    damit ein defektes File nicht den gesamten Import verhindert.
    """
    manifest = read_manifest(directory)
    emails = [
        parse_file(path, received_at=manifest_received_at(manifest, path))
        for path in list_email_files(directory)
    ]
    return sorted(emails, key=lambda mail: mail.received_at)
