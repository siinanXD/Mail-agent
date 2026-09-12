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
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape

from app.email.parser import ParsedEmail

logger = logging.getLogger(__name__)


#: Sekunden fuer den Verbindungsaufbau und jede einzelne Serverantwort. Ohne
#: Grenze haengt ein Server, der die Verbindung annimmt und dann schweigt, den
#: Abruf endlos auf - und mit ihm alle folgenden Postfaecher aller Mandanten.
IMAP_TIMEOUT_SECONDS = 60


class ImapNotConfiguredError(RuntimeError):
    """Es ist kein Postfach hinterlegt."""


class ImapFetchError(RuntimeError):
    """Der Server hat eine Nachricht nicht ausgeliefert (FETCH nicht OK)."""


class ImapSearchError(RuntimeError):
    """Die Suche nach neuen Nachrichten ist fehlgeschlagen (SEARCH nicht OK)."""


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
    #: UID je provider_message_id - damit der Watcher einem fehlgeschlagenen
    #: Import die Stelle im Postfach zuordnen kann.
    uids: dict[str, int] = field(default_factory=dict)
    #: UIDs, die der Server nicht ausgeliefert hat. Sie gelten nicht als verarbeitet.
    failed_uids: list[int] = field(default_factory=list)


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
        uids_by_id: dict[str, int] = {}
        failed_uids: list[int] = []
        for uid in selected:
            try:
                raw = _fetch_one(connection, uid)
            except ImapFetchError as error:
                logger.warning("%s - wird beim naechsten Abruf erneut versucht", error)
                failed_uids.append(uid)
                continue
            if raw is None:
                continue  # zwischen SEARCH und FETCH geloescht - nichts zu holen
            try:
                parsed = parse_message(stdlib_email.message_from_bytes(raw))
            except Exception:
                # Eine kaputte Nachricht darf das Postfach nicht dauerhaft
                # blockieren - der Cursor geht trotzdem an ihr vorbei.
                logger.exception("IMAP-Nachricht UID %s nicht lesbar", uid)
                continue
            emails.append(parsed)
            uids_by_id.setdefault(parsed.provider_message_id, uid)
        return FetchResult(
            emails=sorted(emails, key=lambda mail: mail.received_at),
            uid_validity=validity,
            last_uid=selected[-1] if selected else after,
            remaining=len(uids) - len(selected),
            uids=uids_by_id,
            failed_uids=failed_uids,
        )
    finally:
        _close(connection)


#: Ergebnis eines Verbindungstests. ``status`` ist der Wert, der am Postfach
#: landet (``mailboxes.status``) - die Oberflaeche macht daraus die Ampel.
CHECK_OK = "ok"
#: Zugangsdaten abgelehnt. Heilt nie von selbst, der Kunde muss etwas tun.
CHECK_AUTH_ERROR = "auth_error"
#: Zertifikat oder TLS-Aufbau abgelehnt - auch das ist nichts Voruebergehendes.
CHECK_TLS_ERROR = "tls_error"
#: Ordner gibt es nicht (Tippfehler, oder der Anbieter nennt ihn anders).
CHECK_FOLDER_MISSING = "folder_missing"
#: Server nicht erreichbar. Geht meist von allein wieder.
CHECK_UNREACHABLE = "unreachable"
#: Noch nie geprueft.
CHECK_UNKNOWN = "unknown"

#: Fehler, die der Kunde selbst beheben muss - danach wird nicht stumm weiter
#: probiert, sondern die Oberflaeche zeigt Rot.
PERMANENT_STATUSES = (CHECK_AUTH_ERROR, CHECK_TLS_ERROR, CHECK_FOLDER_MISSING)

#: Anbieter, die fuer IMAP ein eigenes App-Passwort verlangen. Ohne diesen
#: Hinweis sucht man den Fehler beim richtigen Kontopasswort.
_APP_PASSWORD_HOSTS = ("gmail", "google", "outlook", "office365", "hotmail", "live.com", "yahoo")


@dataclass
class ConnectionCheck:
    """Ergebnis von ``check_connection`` - fuer Anzeige und Speicherung."""

    status: str
    #: Klartext fuer die Oberflaeche, ohne Stacktrace und ohne Passwort.
    message: str
    #: Anzahl Mails ab ``config.since`` - nur gefuellt, wenn danach gefragt wurde.
    waiting: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == CHECK_OK

    @property
    def permanent(self) -> bool:
        """Muss jemand eingreifen, oder kann es sich von allein erledigen?"""
        return self.status in PERMANENT_STATUSES


def check_connection(config: ImapConfig, *, count_waiting: bool = False) -> ConnectionCheck:
    """Prueft Zugang und Ordner, ohne eine einzige Mail zu holen.

    Gedacht fuer zwei Stellen: den Knopf "Verbindung testen" in den
    Einstellungen und die regelmaessige Ueberwachung. Beide sollen billig sein -
    Login, Ordner waehlen, fertig. ``count_waiting`` zaehlt zusaetzlich, wie
    viele Mails ab ``since`` im Ordner liegen; das ist die Zahl, die vor dem
    Speichern eines weit zurueckliegenden Datums gezeigt wird.
    """
    try:
        connection = _connect(config)
    except imaplib.IMAP4.error as error:
        return ConnectionCheck(CHECK_AUTH_ERROR, _auth_message(config, error))
    except ssl.SSLError as error:
        return ConnectionCheck(
            CHECK_TLS_ERROR,
            "Die verschluesselte Verbindung kam nicht zustande "
            f"({_short(error)}). Stimmen Server und Port?",
        )
    except OSError as error:
        return ConnectionCheck(
            CHECK_UNREACHABLE,
            f"{config.host}:{config.port} ist nicht erreichbar ({_short(error)})."
            + _port_hint(config),
        )

    try:
        status, _ = connection.select(config.folder, readonly=True)
        if status != "OK":
            return ConnectionCheck(
                CHECK_FOLDER_MISSING,
                f"Der Ordner '{config.folder}' liegt nicht in diesem Postfach.",
            )
        waiting = _count_since(connection, config) if count_waiting else None
    except (imaplib.IMAP4.error, OSError) as error:
        return ConnectionCheck(
            CHECK_UNREACHABLE, f"Der Server hat die Anfrage abgebrochen ({_short(error)})."
        )
    finally:
        _close(connection)

    return ConnectionCheck(CHECK_OK, "Verbindung steht.", waiting=waiting)


def _count_since(connection: imaplib.IMAP4, config: ImapConfig) -> int:
    """Wie viele Mails liegen ab ``since`` im Ordner? (ohne Cursor)"""
    criteria = (
        ["SINCE", config.since.strftime("%d-%b-%Y")] if config.since else ["ALL"]
    )
    status, data = connection.uid("SEARCH", *criteria)
    if status != "OK":
        raise ImapSearchError(f"SEARCH im Ordner {config.folder} fehlgeschlagen ({status})")
    if not data or not data[0]:
        return 0
    return len(data[0].split())


def _auth_message(config: ImapConfig, error: Exception) -> str:
    hint = ""
    if any(name in config.host.lower() for name in _APP_PASSWORD_HOSTS):
        hint = (
            " Bei diesem Anbieter braucht IMAP ein eigenes App-Passwort - "
            "das normale Kontopasswort wird abgelehnt."
        )
    return f"Der Server hat die Zugangsdaten abgelehnt ({_short(error)}).{hint}"


#: Die ueblichen IMAP-Ports. 993 wird gern mit 992 verwechselt - das ist ein
#: ganz anderer Dienst und antwortet nicht, der Versuch laeuft in den Timeout.
IMAP_SSL_PORT = 993
IMAP_PLAIN_PORT = 143


def _port_hint(config: ImapConfig) -> str:
    """Zusatz zur Fehlermeldung, wenn der Port fuer IMAP untypisch ist."""
    erwartet = IMAP_SSL_PORT if config.use_ssl else IMAP_PLAIN_PORT
    if config.port == erwartet:
        return ""
    art = "IMAP mit SSL/TLS" if config.use_ssl else "IMAP ohne SSL"
    return f" Fuer {art} ist ueblicherweise Port {erwartet} richtig."


def _short(error: Exception) -> str:
    """Erste Zeile der Fehlermeldung, gekuerzt - nie das Passwort, nie ein Stacktrace."""
    text = str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__
    return text[:160]


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
    if config.use_ssl:
        # Ausdruecklich pruefender Kontext: Ohne ihn nimmt imaplib einen, der
        # weder Zertifikat noch Hostnamen prueft - ein Angreifer im Netz koennte
        # sich als Mailserver ausgeben und das Passwort beim login() abgreifen.
        connection = imaplib.IMAP4_SSL(
            config.host,
            config.port,
            ssl_context=ssl.create_default_context(),
            timeout=IMAP_TIMEOUT_SECONDS,
        )
    else:
        connection = imaplib.IMAP4(config.host, config.port, timeout=IMAP_TIMEOUT_SECONDS)
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
    if status != "OK":
        # NO/BAD ist kein leeres Postfach: Sonst gaelte der Abruf als erfolgreich
        # und last_error wuerde geleert, obwohl gar nicht gesucht wurde.
        raise ImapSearchError(f"SEARCH im Ordner {config.folder} fehlgeschlagen ({status})")
    if not data or not data[0]:
        return []
    uids = sorted({int(value) for value in data[0].split()})
    # "n:*" liefert laut IMAP immer mindestens die hoechste UID - auch wenn sie
    # kleiner als n ist. Ohne diesen Filter kaeme die letzte Mail immer wieder.
    return [uid for uid in uids if after is None or uid > after]


def _fetch_one(connection: imaplib.IMAP4, uid: int) -> bytes | None:
    """Rohdaten einer Nachricht; None, wenn es sie nicht (mehr) gibt.

    Eine geloeschte UID beantwortet der Server mit OK ohne Daten. NO/BAD heisst
    dagegen "gerade nicht" - das ist ein Fehler, keine leere Nachricht.
    """
    status, data = connection.uid("FETCH", str(uid), "(RFC822)")
    if status != "OK":
        raise ImapFetchError(f"FETCH UID {uid} fehlgeschlagen ({status})")
    if not data:
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
