"""IMAP-Nachrichten in ParsedEmail umwandeln und das Postfach per UID-Cursor abarbeiten."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import EmailMessage

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Mailbox
from app.email import imap_client
from app.email.imap_client import ImapConfig, extract_body, fetch_new_emails, parse_message
from app.email.importer import ImportResult
from app.tenancy import bind_tenant


def build_message(*, html: bool = False) -> EmailMessage:
    message = EmailMessage()
    message["Message-ID"] = "<abc123@mail.example.com>"
    message["From"] = "Marta Novák <m.novak@seznam.cz>"
    message["To"] = "reservierung@ferien.de"
    message["Subject"] = "Änderung unserer Buchung BK-2026-0108"
    message["Date"] = "Sun, 06 Sep 2026 11:15:00 +0200"
    message.set_content("Neue Anreise: 10.09.2026\nGrüße\nMarta")
    if html:
        message.add_alternative(
            "<html><body><p>Neue Anreise: 10.09.2026</p>"
            "<p>Gr&uuml;&szlig;e</p></body></html>",
            subtype="html",
        )
    return message


def test_kopfzeilen_und_umlaute_werden_dekodiert():
    parsed = parse_message(build_message())

    assert parsed.provider_message_id == "<abc123@mail.example.com>"
    assert "Änderung" in parsed.subject
    assert "Novák" in parsed.sender
    assert parsed.received_at.year == 2026
    assert parsed.received_at.month == 9
    assert parsed.received_at.day == 6


def test_textteil_wird_bevorzugt():
    parsed = parse_message(build_message(html=True))

    assert "Neue Anreise: 10.09.2026" in parsed.body
    assert "<p>" not in parsed.body


def test_html_fallback_ohne_textteil():
    message = EmailMessage()
    message["Subject"] = "Nur HTML"
    message["Date"] = "Sun, 06 Sep 2026 11:15:00 +0200"
    message.set_content(
        "<html><body><p>Neue Anreise: 10.09.2026</p>"
        "<p>Gr&uuml;&szlig;e</p></body></html>",
        subtype="html",
    )

    body = extract_body(message)

    assert "Neue Anreise: 10.09.2026" in body
    assert "Grüße" in body
    assert "<" not in body


def test_ersatz_id_ohne_message_id_ist_ueber_prozesse_stabil():
    """Sonst importiert der Watcher dieselbe Mail bei jedem Abruf erneut.

    Der Vergleich laeuft bewusst ueber einen zweiten Python-Prozess: innerhalb
    eines Prozesses waere auch das zufaellig gesaete hash() stabil, und der
    Test wuerde den Fehler nicht bemerken.
    """
    import subprocess
    import sys
    from pathlib import Path

    skript = (
        "from email.message import EmailMessage\n"
        "from app.email.imap_client import parse_message\n"
        "m = EmailMessage()\n"
        "m['Subject'] = 'Ohne ID'\n"
        "m.set_content('Hallo')\n"
        "print(parse_message(m).provider_message_id)\n"
    )
    wurzel = Path(__file__).resolve().parents[1]
    ids = {
        subprocess.run(
            [sys.executable, "-c", skript],
            cwd=wurzel,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for _ in range(2)
    }

    assert len(ids) == 1
    ersatz_id = ids.pop()
    assert ersatz_id.startswith("imap-")
    assert len(ersatz_id) == len("imap-") + 32


def test_fehlende_kopfzeilen_brechen_nicht():
    message = EmailMessage()
    message.set_content("Hallo")

    parsed = parse_message(message)

    assert parsed.provider_message_id  # Fallback-ID
    assert parsed.subject == ""
    assert parsed.body == "Hallo"


# ---------------------------------------------------------------- UID-Cursor


class FakeImap:
    """Minimaler IMAP-Server fuer die UID-Befehle, die der Client nutzt."""

    def __init__(self, uids: list[int], validity: int = 7) -> None:
        self.uids = sorted(uids)
        self.validity = validity
        self.fetched: list[int] = []

    def select(self, folder: str, readonly: bool = False):
        return "OK", [str(len(self.uids)).encode()]

    def response(self, code: str):
        if code == "UIDVALIDITY":
            return code, [str(self.validity).encode()]
        return code, [None]

    def uid(self, command: str, *args):
        if command == "SEARCH":
            if args and args[0] == "UID":
                low = int(args[1].split(":")[0])
                found = [uid for uid in self.uids if uid >= low]
                # Wie ein echter Server: "n:*" liefert immer mindestens die
                # hoechste UID, auch wenn sie kleiner als n ist.
                if not found and self.uids:
                    found = [self.uids[-1]]
            else:
                found = list(self.uids)
            return "OK", [" ".join(str(uid) for uid in found).encode()]
        if command == "FETCH":
            uid = int(args[0])
            self.fetched.append(uid)
            message = EmailMessage()
            message["Message-ID"] = f"<uid-{uid}@test>"
            message["Subject"] = f"Mail {uid}"
            message["Date"] = "Sun, 06 Sep 2026 11:15:00 +0200"
            message.set_content("Hallo")
            return "OK", [(f"{uid} (RFC822)".encode(), message.as_bytes()), b")"]
        raise AssertionError(f"unerwarteter Befehl {command}")

    def close(self) -> None:
        pass

    def logout(self) -> None:
        pass


def _config(**overrides) -> ImapConfig:
    return ImapConfig(host="imap.test", username="u", password="p", batch_size=50, **overrides)


def _ids(result) -> set[int]:
    return {int(mail.provider_message_id.split("-")[1].split("@")[0]) for mail in result.emails}


@pytest.fixture
def server(monkeypatch):
    fake = FakeImap(list(range(1, 121)))  # 120 Mails im Postfach
    monkeypatch.setattr(imap_client, "_connect", lambda config: fake)
    return fake


def test_rueckstau_wird_von_den_aeltesten_an_abgearbeitet(server):
    """Frueher kamen immer nur die neuesten 50 - die aelteren 70 nie."""
    first = fetch_new_emails(_config())

    assert _ids(first) == set(range(1, 51))
    assert (first.last_uid, first.uid_validity, first.remaining) == (50, 7, 70)

    second = fetch_new_emails(_config(last_uid=first.last_uid, uid_validity=7))
    assert _ids(second) == set(range(51, 101))
    assert second.remaining == 20

    third = fetch_new_emails(_config(last_uid=second.last_uid, uid_validity=7))
    assert _ids(third) == set(range(101, 121))
    assert (third.last_uid, third.remaining) == (120, 0)


def test_ohne_neue_mails_kommt_die_letzte_nicht_erneut(server):
    """Die "n:*"-Eigenheit von IMAP darf die hoechste UID nicht wieder liefern."""
    result = fetch_new_emails(_config(last_uid=120, uid_validity=7))

    assert result.emails == []
    assert (result.last_uid, result.remaining) == (120, 0)
    assert server.fetched == []


def test_neue_mail_nach_dem_cursor_wird_geholt(server):
    server.uids.append(121)

    result = fetch_new_emails(_config(last_uid=120, uid_validity=7))

    assert _ids(result) == {121}
    assert result.last_uid == 121


def test_geaenderte_uidvalidity_beginnt_von_vorn(server):
    """Andere UIDVALIDITY = UIDs sind neu vergeben, der alte Cursor ist wertlos."""
    result = fetch_new_emails(_config(last_uid=100, uid_validity=5))

    assert _ids(result) == set(range(1, 51))
    assert result.uid_validity == 7


def test_watcher_arbeitet_rueckstau_in_einem_abruf_ab_und_merkt_sich_den_cursor(
    sqlite_engine, server, monkeypatch
):
    watcher = importlib.import_module("app.email.watcher")
    make_session = sessionmaker(bind=sqlite_engine, expire_on_commit=False)

    with make_session() as setup:
        setup.add(
            Mailbox(
                id=1, tenant_id=1, host="imap.test", port=993, username="u",
                password_encrypted="verschluesselt", folder="INBOX", use_ssl=True, active=True,
            )
        )
        setup.commit()

    @contextmanager
    def plain_scope() -> Iterator[Session]:
        db = make_session()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @contextmanager
    def tenant_scope(tenant_id: int | None = None) -> Iterator[Session]:
        db = bind_tenant(make_session(), tenant_id)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    imported_batches: list[int] = []

    def fake_import(session, emails):
        imported_batches.append(len(emails))
        return ImportResult(imported=len(emails))

    monkeypatch.setattr(watcher, "session_scope", plain_scope)
    monkeypatch.setattr(watcher, "tenant_session", tenant_scope)
    monkeypatch.setattr(watcher, "decrypt_secret", lambda token: "p")
    monkeypatch.setattr(watcher, "import_emails", fake_import)

    outcome = watcher.poll_mailbox(1)

    assert outcome.error is None
    assert imported_batches == [50, 50, 20]
    assert outcome.result.imported == 120
    with make_session() as check:
        mailbox = check.get(Mailbox, 1)
        assert (mailbox.last_uid, mailbox.uid_validity) == (120, 7)

    # Zweiter Abruf ohne neue Mails: nichts importiert, Cursor bleibt stehen.
    imported_batches.clear()
    watcher.poll_mailbox(1)
    assert imported_batches == [0]
    with make_session() as check:
        assert check.get(Mailbox, 1).last_uid == 120


def test_fehlgeschlagener_import_schiebt_den_cursor_nicht_weiter(
    sqlite_engine, server, monkeypatch
):
    """Sonst waeren die Mails eines gescheiterten Batches verloren."""
    watcher = importlib.import_module("app.email.watcher")
    make_session = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    with make_session() as setup:
        setup.add(
            Mailbox(
                id=2, tenant_id=1, host="imap.test", port=993, username="u",
                password_encrypted="verschluesselt", folder="INBOX", use_ssl=True, active=True,
            )
        )
        setup.commit()

    @contextmanager
    def scope(*args, **kwargs) -> Iterator[Session]:
        db = make_session()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def broken_import(session, emails):
        raise RuntimeError("LLM nicht erreichbar")

    monkeypatch.setattr(watcher, "session_scope", scope)
    monkeypatch.setattr(watcher, "tenant_session", scope)
    monkeypatch.setattr(watcher, "decrypt_secret", lambda token: "p")
    monkeypatch.setattr(watcher, "import_emails", broken_import)

    outcome = watcher.poll_mailbox(2)

    assert outcome.error and "LLM nicht erreichbar" in outcome.error
    with make_session() as check:
        assert check.get(Mailbox, 2).last_uid is None
