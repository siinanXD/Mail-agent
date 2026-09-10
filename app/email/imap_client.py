"""IMAP-Anbindung: holt neue Nachrichten und macht ParsedEmail daraus.

Nutzt die Standardbibliothek (imaplib, email) - keine zusaetzliche Dependency.
Hinweis: ``import email`` trifft hier das stdlib-Paket, nicht ``app.email``,
weil Python 3 absolute Importe verwendet.

Die Verbindungsdaten kommen als ``ImapConfig`` herein - im Mehrmandantenbetrieb
aus der Tabelle ``mailboxes``, nicht mehr aus der .env.
"""

from __future__ import annotations

import email as stdlib_email
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape

from app.email.parser import ParsedEmail

logger = logging.getLogger(__name__)


class ImapNotConfiguredError(RuntimeError):
    """Es ist kein Postfach hinterlegt."""


@dataclass
class ImapConfig:
    host: str
    username: str
    password: str
    port: int = 993
    folder: str = "INBOX"
    use_ssl: bool = True
    since: date | None = None
    batch_size: int = 50

    def __repr__(self) -> str:
        # Das Passwort taucht nie in Logs oder Tracebacks auf.
        return f"ImapConfig({self.username}@{self.host}:{self.port}/{self.folder})"


def fetch_new_emails(config: ImapConfig) -> list[ParsedEmail]:
    """Holt Nachrichten aus dem Ordner des Postfachs.

    Es wird nichts als gelesen markiert und nichts geloescht - welche Mails neu
    sind, entscheidet die Datenbank ueber die provider_message_id.
    """
    connection = _connect(config)
    try:
        connection.select(config.folder, readonly=True)
        message_ids = _search(connection, config)
        # Die neuesten zuerst suchen, aber chronologisch verarbeiten.
        selected = message_ids[-config.batch_size :]

        emails: list[ParsedEmail] = []
        for message_id in selected:
            raw = _fetch_one(connection, message_id)
            if raw is None:
                continue
            try:
                emails.append(parse_message(stdlib_email.message_from_bytes(raw)))
            except Exception:
                logger.exception("IMAP-Nachricht %s nicht lesbar", message_id)
        return sorted(emails, key=lambda mail: mail.received_at)
    finally:
        _close(connection)


def parse_message(message: Message) -> ParsedEmail:
    """Wandelt eine RFC-822-Nachricht in unser ParsedEmail um."""
    return ParsedEmail(
        provider_message_id=_header(message, "Message-ID") or _stable_id(message),
        sender=_header(message, "From"),
        recipient=_header(message, "To"),
        subject=_header(message, "Subject"),
        body=extract_body(message),
        received_at=_received_at(message),
    )


def _stable_id(message: Message) -> str:
    """Ersatz-ID fuer Mails ohne Message-ID - ueber Prozesse hinweg gleich.

    Nicht ``hash()``: der ist pro Python-Prozess zufaellig (PYTHONHASHSEED).
    Dieselbe Mail bekaeme bei jedem Abruf eine andere ID, wuerde die
    Dublettenpruefung passieren und jedes Mal neu importiert - samt LLM-Call.
    """
    try:
        raw = message.as_bytes()
    except Exception:
        raw = message.as_string().encode("utf-8", errors="replace")
    return f"imap-{hashlib.sha256(raw).hexdigest()[:32]}"


def extract_body(message: Message) -> str:
    """Bevorzugt text/plain; faellt auf text/html mit entfernten Tags zurueck."""
    plain, html = "", ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue  # Anhaenge ignorieren
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        text = _decode_part(part)
        if content_type == "text/plain" and not plain:
            plain = text
        elif content_type == "text/html" and not html:
            html = text

    if plain.strip():
        return plain.strip()
    return _strip_html(html).strip()


def _connect(config: ImapConfig) -> imaplib.IMAP4:
    factory = imaplib.IMAP4_SSL if config.use_ssl else imaplib.IMAP4
    connection = factory(config.host, config.port)
    connection.login(config.username, config.password)
    return connection


def _close(connection: imaplib.IMAP4) -> None:
    try:
        connection.close()
    except Exception:
        pass
    try:
        connection.logout()
    except Exception:
        pass


def _search(connection: imaplib.IMAP4, config: ImapConfig) -> list[bytes]:
    criteria: list[str] = ["ALL"]
    if config.since:
        criteria = ["SINCE", config.since.strftime("%d-%b-%Y")]

    status, data = connection.search(None, *criteria)
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _fetch_one(connection: imaplib.IMAP4, message_id: bytes) -> bytes | None:
    status, data = connection.fetch(message_id, "(RFC822)")
    if status != "OK" or not data:
        return None
    for part in data:
        if isinstance(part, tuple) and len(part) > 1:
            return part[1]
    return None


def _header(message: Message, name: str) -> str:
    raw = message.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return str(raw).strip()


def _received_at(message: Message) -> datetime:
    raw = message.get("Date")
    if not raw:
        return datetime.utcnow()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return datetime.utcnow()
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]+", " ", text)
