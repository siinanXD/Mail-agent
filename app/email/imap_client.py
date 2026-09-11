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
    #: Hoechste bereits verarbeitete UID und die UIDVALIDITY, zu der sie gehoert.
    last_uid: int | None = None
    uid_validity: int | None = None

    def __repr__(self) -> str:
        # Das Passwort taucht nie in Logs oder Tracebacks auf.
        return f"ImapConfig({self.username}@{self.host}:{self.port}/{self.folder})"


@dataclass
class FetchResult:
    """Ein Abruf plus der Cursor fuer den naechsten."""

    emails: list[ParsedEmail]
    uid_validity: int | None
    last_uid: int | None
    #: Weitere neue Nachrichten, die nicht mehr in diesen Batch gepasst haben.
    remaining: int = 0


def fetch_new_emails(config: ImapConfig) -> FetchResult:
    """Holt die naechsten noch nicht verarbeiteten Nachrichten des Postfachs.

    Gearbeitet wird mit IMAP-UIDs statt Sequenznummern: ``config.last_uid`` ist
    die hoechste bereits verarbeitete UID. Geholt werden die AELTESTEN
    ``batch_size`` Nachrichten darueber. So arbeitet sich der Watcher durch einen
    Rueckstau, statt immer nur die neuesten zu sehen und aeltere nie zu erreichen.

    Es wird nichts als gelesen markiert und nichts geloescht.
    """
    connection = _connect(config)
    try:
        connection.select(config.folder, readonly=True)
        validity = _uid_validity(connection)
        # Der Cursor gilt nur unter derselben UIDVALIDITY - sonst von vorn.
        after = (
            config.last_uid
            if validity is not None and validity == config.uid_validity
            else None
        )
        uids = _search_uids(connection, config, after)
        selected = uids[: config.batch_size]

        emails: list[ParsedEmail] = []
        for uid in selected:
            raw = _fetch_one(connection, uid)
            if raw is None:
                continue
            try:
                emails.append(parse_message(stdlib_email.message_from_bytes(raw)))
            except Exception:
                # Eine kaputte Nachricht darf das Postfach nicht dauerhaft
                # blockieren - der Cursor geht trotzdem an ihr vorbei.
                logger.exception("IMAP-Nachricht UID %s nicht lesbar", uid)
        return FetchResult(
            emails=sorted(emails, key=lambda mail: mail.received_at),
            uid_validity=validity,
            last_uid=selected[-1] if selected else after,
            remaining=len(uids) - len(selected),
        )
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


def _uid_validity(connection: imaplib.IMAP4) -> int | None:
    _, data = connection.response("UIDVALIDITY")
    for value in data or []:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _search_uids(
    connection: imaplib.IMAP4, config: ImapConfig, after: int | None
) -> list[int]:
    """UIDs der noch nicht verarbeiteten Nachrichten, aufsteigend."""
    criteria: list[str] = []
    if after is not None:
        criteria += ["UID", f"{after + 1}:*"]
    if config.since:
        criteria += ["SINCE", config.since.strftime("%d-%b-%Y")]

    status, data = connection.uid("SEARCH", *(criteria or ["ALL"]))
    if status != "OK" or not data or not data[0]:
        return []
    uids = sorted({int(value) for value in data[0].split()})
    # "n:*" liefert laut IMAP immer mindestens die hoechste UID - auch wenn sie
    # kleiner als n ist. Ohne diesen Filter kaeme die letzte Mail immer wieder.
    return [uid for uid in uids if after is None or uid > after]


def _fetch_one(connection: imaplib.IMAP4, uid: int) -> bytes | None:
    status, data = connection.uid("FETCH", str(uid), "(RFC822)")
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
