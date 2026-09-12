"""Postfach einstellen, Verbindung pruefen, Laeufe anzeigen."""

from __future__ import annotations

import imaplib
import importlib
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import crypto
from app.crypto import decrypt_secret, generate_key
from app.database import accounts
from app.database.models import RUN_OK, RUN_POLL, AgentRun, Mailbox
from app.email.imap_client import (
    CHECK_AUTH_ERROR,
    CHECK_FOLDER_MISSING,
    CHECK_OK,
    CHECK_TLS_ERROR,
    CHECK_UNREACHABLE,
    ConnectionCheck,
    ImapConfig,
    check_connection,
)
from app.main import app

PASSWORT = "sehr-geheim-123"
NUTZER_1 = "anna@standard.de"
NUTZER_2 = "bernd@zweiter.de"

ZUGANG = {
    "host": "imap.example.de",
    "username": "post@example.de",
    "password": "imap-geheim",
    "port": 993,
    "folder": "INBOX",
    "use_ssl": True,
    "since_date": "2026-09-01",
    "active": True,
}


@pytest.fixture(autouse=True)
def verschluesselung(monkeypatch):
    """Fester Schluessel fuer die Tests - sonst scheitert das Speichern."""
    schluessel = generate_key()
    monkeypatch.setattr(
        crypto, "get_settings", lambda: type("S", (), {"encryption_key": schluessel})()
    )


@pytest.fixture
def api(session, sqlite_engine, monkeypatch):
    """Angemeldete Clients fuer beide Mandanten, alles auf der Test-Datenbank."""
    accounts.create_user(session, tenant_id=1, email=NUTZER_1, password=PASSWORT)
    accounts.create_user(session, tenant_id=2, email=NUTZER_2, password=PASSWORT)
    session.commit()

    make_session = sessionmaker(bind=sqlite_engine, expire_on_commit=False)

    @contextmanager
    def scope() -> Iterator[Session]:
        db = make_session()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    for name in ("app.api.auth", "app.api.mailbox", "app.email.watcher"):
        monkeypatch.setattr(importlib.import_module(name), "session_scope", scope)
    monkeypatch.setattr(
        importlib.import_module("app.api.auth").login_throttle, "_events", {}
    )

    offen: list[TestClient] = []

    def anmelden(email: str) -> TestClient:
        client = TestClient(app)
        client.__enter__()
        offen.append(client)
        antwort = client.post("/api/login", json={"email": email, "password": PASSWORT})
        assert antwort.status_code == 200, antwort.text
        return client

    anmelden.scope = scope  # type: ignore[attr-defined]
    yield anmelden
    for client in offen:
        client.__exit__(None, None, None)


@pytest.fixture
def client(api) -> TestClient:
    return api(NUTZER_1)


@pytest.fixture
def db(api) -> Iterator[Session]:
    with api.scope() as session:
        yield session


@pytest.fixture
def verbindung_ok(monkeypatch) -> list[ImapConfig]:
    """Ersetzt den echten IMAP-Test - kein Netzwerk in den Tests."""
    versuche: list[ImapConfig] = []

    def fake(config: ImapConfig, *, count_waiting: bool = False) -> ConnectionCheck:
        versuche.append(config)
        return ConnectionCheck(
            CHECK_OK, "Verbindung steht.", waiting=42 if count_waiting else None
        )

    for name in ("app.api.mailbox", "app.email.watcher"):
        monkeypatch.setattr(importlib.import_module(name), "check_connection", fake)
    return versuche


# ---------------------------------------------------------------- Einstellungen


def test_ohne_postfach_meldet_die_oberflaeche_nicht_eingerichtet(client):
    antwort = client.get("/api/mailbox").json()

    assert antwort["configured"] is False
    assert antwort["status"] == "unknown"


def test_postfach_anlegen_verschluesselt_das_passwort(client, db, verbindung_ok):
    antwort = client.put("/api/mailbox", json=ZUGANG)

    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["configured"] is True
    assert daten["host"] == "imap.example.de"
    # Das Passwort verlaesst den Server nie wieder.
    assert "imap-geheim" not in antwort.text
    gespeichert = db.scalar(select(Mailbox))
    assert gespeichert.password_encrypted != "imap-geheim"
    assert decrypt_secret(gespeichert.password_encrypted) == "imap-geheim"


def test_speichern_prueft_die_verbindung_sofort(client, verbindung_ok):
    daten = client.put("/api/mailbox", json=ZUGANG).json()

    assert daten["status"] == "ok"
    assert daten["last_check_at"] is not None


def test_leeres_passwortfeld_laesst_das_gespeicherte_stehen(client, db, verbindung_ok):
    client.put("/api/mailbox", json=ZUGANG)

    client.put("/api/mailbox", json={**ZUGANG, "password": None, "folder": "Archiv"})

    gespeichert = db.scalar(select(Mailbox))
    assert gespeichert.folder == "Archiv"
    assert decrypt_secret(gespeichert.password_encrypted) == "imap-geheim"


def test_neues_postfach_ohne_passwort_wird_abgelehnt(client, verbindung_ok):
    antwort = client.put("/api/mailbox", json={**ZUGANG, "password": None})

    assert antwort.status_code == 400
    assert "Passwort" in antwort.json()["detail"]


# ---------------------------------------------------------------- Rueckblick-Datum


def test_datum_zurueck_setzt_den_cursor_zurueck(client, db, verbindung_ok):
    """Ohne das haelt last_uid den Abruf fest - die aelteren Mails kaemen nie."""
    client.put("/api/mailbox", json=ZUGANG)
    gespeichert = db.scalar(select(Mailbox))
    gespeichert.last_uid, gespeichert.uid_validity = 500, 7
    db.commit()

    client.put("/api/mailbox", json={**ZUGANG, "password": None, "since_date": "2026-01-01"})

    db.expire_all()
    gespeichert = db.scalar(select(Mailbox))
    assert gespeichert.last_uid is None
    assert gespeichert.uid_validity is None


def test_datum_nach_vorn_laesst_den_cursor_stehen(client, db, verbindung_ok):
    client.put("/api/mailbox", json=ZUGANG)
    gespeichert = db.scalar(select(Mailbox))
    gespeichert.last_uid = 500
    db.commit()

    client.put("/api/mailbox", json={**ZUGANG, "password": None, "since_date": "2026-09-20"})

    db.expire_all()
    assert db.scalar(select(Mailbox)).last_uid == 500


def test_rescan_entscheidung_haengt_an_postfach_ordner_und_datum():
    mailbox = Mailbox(
        host="imap.example.de",
        username="post@example.de",
        folder="INBOX",
        since_date=date(2026, 9, 1),
    )
    unveraendert = dict(host="imap.example.de", username="post@example.de", folder="INBOX")

    assert not accounts.needs_rescan(mailbox, **unveraendert, since_date=date(2026, 9, 1))
    assert not accounts.needs_rescan(mailbox, **unveraendert, since_date=date(2026, 9, 5))
    assert accounts.needs_rescan(mailbox, **unveraendert, since_date=date(2026, 8, 1))
    # "alles lesen" ist der weiteste Rueckblick ueberhaupt.
    assert accounts.needs_rescan(mailbox, **unveraendert, since_date=None)
    assert accounts.needs_rescan(
        mailbox, host="imap.example.de", username="post@example.de", folder="Archiv",
        since_date=date(2026, 9, 1),
    )


# ---------------------------------------------------------------- Verbindungstest


def test_test_endpunkt_nutzt_das_gespeicherte_passwort(client, verbindung_ok):
    client.put("/api/mailbox", json=ZUGANG)
    verbindung_ok.clear()

    antwort = client.post(
        "/api/mailbox/test?count_waiting=true", json={**ZUGANG, "password": None}
    )

    assert antwort.status_code == 200
    assert antwort.json()["waiting"] == 42
    assert verbindung_ok[-1].password == "imap-geheim"


def test_test_endpunkt_speichert_nichts(client, db, verbindung_ok):
    client.put("/api/mailbox", json=ZUGANG)

    client.post("/api/mailbox/test", json={**ZUGANG, "password": None, "host": "imap.anders.de"})

    db.expire_all()
    assert db.scalar(select(Mailbox)).host == "imap.example.de"


@pytest.mark.parametrize(
    "fehler, erwartet",
    [
        (imaplib.IMAP4.error("AUTHENTICATIONFAILED"), CHECK_AUTH_ERROR),
        (ssl.SSLError("certificate verify failed"), CHECK_TLS_ERROR),
        (TimeoutError("timed out"), CHECK_UNREACHABLE),
        (OSError("Name or service not known"), CHECK_UNREACHABLE),
    ],
)
def test_verbindungsfehler_werden_unterschieden(monkeypatch, fehler, erwartet):
    """Ein abgelehntes Passwort heilt nie von selbst, ein weggebrochener Server schon."""
    modul = importlib.import_module("app.email.imap_client")
    monkeypatch.setattr(modul, "_connect", lambda config: (_ for _ in ()).throw(fehler))

    ergebnis = check_connection(
        ImapConfig(host="imap.example.de", username="u", password="p")
    )

    assert ergebnis.status == erwartet
    assert ergebnis.ok is False
    assert ergebnis.permanent is (erwartet in (CHECK_AUTH_ERROR, CHECK_TLS_ERROR))
    # Kein Stacktrace und kein Passwort in der Meldung.
    assert "Traceback" not in ergebnis.message
    assert "p" not in ergebnis.message.split()


def test_app_passwort_hinweis_bei_gmail(monkeypatch):
    modul = importlib.import_module("app.email.imap_client")
    monkeypatch.setattr(
        modul,
        "_connect",
        lambda config: (_ for _ in ()).throw(imaplib.IMAP4.error("Invalid credentials")),
    )

    ergebnis = check_connection(
        ImapConfig(host="imap.gmail.com", username="u@gmail.com", password="p")
    )

    assert "App-Passwort" in ergebnis.message


def test_fehlender_ordner_ist_kein_netzwerkproblem(monkeypatch):
    class FakeVerbindung:
        def select(self, folder, readonly=False):
            return "NO", [b"Mailbox doesn't exist"]

        def close(self):
            pass

        def logout(self):
            pass

    modul = importlib.import_module("app.email.imap_client")
    monkeypatch.setattr(modul, "_connect", lambda config: FakeVerbindung())

    ergebnis = check_connection(
        ImapConfig(host="imap.example.de", username="u", password="p", folder="Gibtsnicht")
    )

    assert ergebnis.status == CHECK_FOLDER_MISSING
    assert "Gibtsnicht" in ergebnis.message
    assert ergebnis.permanent is True


# ---------------------------------------------------------------- Mandantentrennung


def test_fremdes_postfach_bleibt_unsichtbar(api, verbindung_ok):
    erster = api(NUTZER_1)
    erster.put("/api/mailbox", json=ZUGANG)

    zweiter = api(NUTZER_2)

    assert zweiter.get("/api/mailbox").json()["configured"] is False


def test_zweiter_mandant_ueberschreibt_das_fremde_postfach_nicht(api, db, verbindung_ok):
    api(NUTZER_1).put("/api/mailbox", json=ZUGANG)

    api(NUTZER_2).put("/api/mailbox", json={**ZUGANG, "host": "imap.zweiter.de"})

    hosts = {m.tenant_id: m.host for m in db.scalars(select(Mailbox))}
    assert hosts == {1: "imap.example.de", 2: "imap.zweiter.de"}


def test_ohne_anmeldung_kein_zugriff_auf_das_postfach(api):
    anonym = TestClient(app)

    assert anonym.get("/api/mailbox").status_code == 401
    assert anonym.put("/api/mailbox", json=ZUGANG).status_code == 401
    assert anonym.post("/api/mailbox/test").status_code == 401
    assert anonym.post("/api/mailbox/poll").status_code == 401
    assert anonym.get("/api/activity").status_code == 401


# ---------------------------------------------------------------- Aktivitaet


def test_aktivitaet_zeigt_nur_die_eigenen_laeufe(api, db, verbindung_ok):
    erster = api(NUTZER_1)
    erster.put("/api/mailbox", json=ZUGANG)
    lauf = accounts.start_run(db, tenant_id=1, mailbox_id=None, kind=RUN_POLL)
    accounts.finish_run(db, lauf, status=RUN_OK, message="3 neue Mail(s)", imported=3)
    fremd = accounts.start_run(db, tenant_id=2, mailbox_id=None, kind=RUN_POLL)
    accounts.finish_run(db, fremd, status=RUN_OK, message="Nur fuer Mandant 2")
    db.commit()

    eigene = erster.get("/api/activity").json()
    andere = api(NUTZER_2).get("/api/activity").json()

    assert [lauf["message"] for lauf in eigene["runs"]] == ["3 neue Mail(s)"]
    assert [lauf["message"] for lauf in andere["runs"]] == ["Nur fuer Mandant 2"]
    assert eigene["busy"] is False


def test_laufender_abruf_wird_als_beschaeftigt_gemeldet(client, db, verbindung_ok):
    client.put("/api/mailbox", json=ZUGANG)
    accounts.start_run(db, tenant_id=1, mailbox_id=None, kind=RUN_POLL)
    db.commit()

    assert client.get("/api/activity").json()["busy"] is True


def test_abruf_anstossen_startet_im_hintergrund(client, db, monkeypatch, verbindung_ok):
    client.put("/api/mailbox", json=ZUGANG)
    angestossen: list[int] = []
    monkeypatch.setattr(
        importlib.import_module("app.api.mailbox").watcher,
        "poll_mailbox",
        lambda mailbox_id: angestossen.append(mailbox_id),
    )

    antwort = client.post("/api/mailbox/poll")

    assert antwort.status_code == 202
    assert angestossen == [db.scalar(select(Mailbox)).id]


def test_abruf_ohne_postfach_meldet_404(client):
    assert client.post("/api/mailbox/poll").status_code == 404


def test_haengende_laeufe_werden_nach_einem_neustart_abgeraeumt(db):
    """Sonst dreht sich die Anzeige ewig, obwohl niemand mehr arbeitet."""
    alt = accounts.start_run(db, tenant_id=1, mailbox_id=None, kind=RUN_POLL)
    db.get(AgentRun, alt).started_at -= timedelta(hours=3)
    frisch = accounts.start_run(db, tenant_id=1, mailbox_id=None, kind=RUN_POLL)
    db.commit()

    accounts.abandon_stale_runs(db, older_than=timedelta(hours=1))

    assert db.get(AgentRun, alt).status == "error"
    assert db.get(AgentRun, frisch).status == "running"


def test_gescheiterter_abruf_erklaert_sich_verstaendlich(session, monkeypatch):
    """Im Aktivitaetsstreifen soll kein "gaierror: [Errno 11001]" stehen."""
    watcher = importlib.import_module("app.email.watcher")

    @contextmanager
    def scope() -> Iterator[Session]:
        yield session
        session.commit()

    monkeypatch.setattr(watcher, "session_scope", scope)
    monkeypatch.setattr(watcher, "tenant_session", lambda tenant_id=None: scope())
    monkeypatch.setattr(
        watcher,
        "fetch_new_emails",
        lambda config: (_ for _ in ()).throw(OSError("[Errno 11001] getaddrinfo failed")),
    )
    monkeypatch.setattr(
        watcher,
        "check_connection",
        lambda config, count_waiting=False: ConnectionCheck(
            CHECK_AUTH_ERROR, "Der Server hat die Zugangsdaten abgelehnt."
        ),
    )
    mailbox = accounts.add_mailbox(
        session,
        tenant_id=1,
        host="imap.example.de",
        username="post@example.de",
        password="imap-geheim",
    )
    session.commit()

    ergebnis = watcher.poll_mailbox(mailbox.id)

    lauf = session.scalars(select(AgentRun)).all()[-1]
    assert lauf.status == "error"
    assert lauf.message == "Der Server hat die Zugangsdaten abgelehnt."
    # Der Postfachstatus wird gleich mit richtiggestellt - sonst stuende die
    # Ampel bis zum naechsten Testlauf auf Gruen.
    assert session.get(Mailbox, mailbox.id).status == CHECK_AUTH_ERROR
    # Die technische Ursache bleibt fuer die Fehlersuche erhalten.
    assert "getaddrinfo" in ergebnis.error
