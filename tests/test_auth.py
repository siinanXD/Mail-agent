"""Selbstregistrierung, Bestaetigungscode, Passwort-Reset und dauerhafte Sitzungen."""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database import accounts
from app.database.models import LoginSession, User, VerificationCode, utcnow
from app.main import app

PASSWORT = "sehr-geheim-123"
NEU = "anna@fewo-nord.de"
BESTAND = "bernd@bestand.de"


@dataclass
class Mail:
    to: str
    subject: str
    body: str

    @property
    def code(self) -> str:
        treffer = re.search(r"\b(\d{6})\b", self.body)
        assert treffer, f"Kein Code in der Mail: {self.body}"
        return treffer.group(1)


@pytest.fixture
def post(session, sqlite_engine, monkeypatch):
    """Client-Fabrik gegen die SQLite-Testdatenbank, plus abgefangene Mails.

    Ein bestehender, bestaetigter Nutzer liegt schon im Mandanten 1 - an ihm
    haengen die Faelle "Adresse schon vergeben" und "Passwort vergessen".
    """
    accounts.create_user(session, tenant_id=1, email=BESTAND, password=PASSWORT)
    session.commit()

    make_session = sessionmaker(bind=sqlite_engine, expire_on_commit=False)

    @contextmanager
    def plain_scope() -> Iterator[Session]:
        db = make_session()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    monkeypatch.setattr(
        importlib.import_module("app.api.auth"), "session_scope", plain_scope
    )
    auth = importlib.import_module("app.api.auth")
    # Frische Zaehler - sonst haengt das Ergebnis an der Reihenfolge der Tests.
    monkeypatch.setattr(auth.login_throttle, "_events", {})
    monkeypatch.setattr(auth.code_throttle, "_events", {})
    monkeypatch.setattr(auth.code_ip_throttle, "_events", {})

    yield make_session


@pytest.fixture
def mails(monkeypatch) -> list[Mail]:
    """Faengt den Versand ab - ohne SMTP, aber ueber die echten Textbausteine."""
    posteingang: list[Mail] = []
    monkeypatch.setattr(
        importlib.import_module("app.notify"),
        "send_mail",
        lambda to, subject, body: posteingang.append(Mail(to, subject, body)),
    )
    return posteingang


@pytest.fixture
def client(post) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _registrieren(client: TestClient, email: str = NEU, **felder) -> dict:
    daten = {"email": email, "password": PASSWORT, "company": "Fewo Nord"} | felder
    return client.post("/api/register", json=daten)


# ---------------------------------------------------------------- Registrierung


def test_registrierung_legt_eigenen_mandanten_an_und_meldet_nach_code_an(
    client, mails, post
):
    assert _registrieren(client).status_code == 202

    # Vor dem Code kommt niemand rein.
    gesperrt = client.post("/api/login", json={"email": NEU, "password": PASSWORT})
    assert gesperrt.status_code == 403
    assert gesperrt.json()["detail"]["reason"] == "unverified"

    bestaetigt = client.post("/api/verify", json={"email": NEU, "code": mails[0].code})
    assert bestaetigt.status_code == 200
    assert bestaetigt.json()["tenant_name"] == "Fewo Nord"
    # Und die Sitzung steht sofort - ohne zweite Anmeldung.
    assert client.get("/api/session").json()["email"] == NEU

    with post() as db:
        neuer = db.scalar(select(User).where(User.email == NEU))
        assert neuer.verified_at is not None
        # Eigener Mandant, nicht der des bestehenden Nutzers.
        assert neuer.tenant.slug == "fewo-nord"
        assert neuer.tenant_id != db.scalar(
            select(User.tenant_id).where(User.email == BESTAND)
        )


def test_registrierung_verraet_nicht_ob_die_adresse_schon_vergeben_ist(client, mails):
    neu = _registrieren(client, email=NEU)
    vergeben = _registrieren(client, email=BESTAND)

    assert neu.status_code == vergeben.status_code == 202
    assert neu.json() == vergeben.json()
    # Den Unterschied erfaehrt nur das Postfach: kein Code fuer eine bekannte Adresse.
    assert "bestaetigen" in mails[0].subject.lower()
    assert mails[1].to == BESTAND
    assert not re.search(r"\b\d{6}\b", mails[1].body)


def test_zu_kurzes_passwort_wird_abgelehnt(client, mails):
    antwort = _registrieren(client, password="kurz")

    assert antwort.status_code == 400
    assert mails == []


def test_ungueltige_adresse_wird_abgelehnt(client, mails):
    assert _registrieren(client, email="keine-adresse").status_code == 400
    assert mails == []


def test_neuer_code_entwertet_den_alten(client, mails):
    _registrieren(client)
    alt = mails[0].code

    client.post("/api/register/resend", json={"email": NEU})
    neu = mails[1].code

    assert client.post("/api/verify", json={"email": NEU, "code": alt}).status_code == 400
    assert client.post("/api/verify", json={"email": NEU, "code": neu}).status_code == 200


def test_falscher_code_ist_nach_fuenf_versuchen_verbraucht(client, mails, post):
    _registrieren(client)
    richtig = mails[0].code

    for _ in range(accounts.MAX_CODE_ATTEMPTS):
        assert (
            client.post("/api/verify", json={"email": NEU, "code": "000000"}).status_code
            == 400
        )

    # Die IP-Bremse aus dem Weg raeumen: geprueft wird der Zaehler am Code selbst,
    # der auch dann greift, wenn der Angreifer die IP wechselt.
    importlib.import_module("app.api.auth").login_throttle._events.clear()

    # Auch der richtige Code hilft jetzt nicht mehr - es braucht einen neuen.
    assert (
        client.post("/api/verify", json={"email": NEU, "code": richtig}).status_code
        == 400
    )


def test_abgelaufener_code_gilt_nicht_mehr(client, mails, post):
    _registrieren(client)
    with post() as db:
        code = db.scalar(select(VerificationCode))
        code.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    antwort = client.post("/api/verify", json={"email": NEU, "code": mails[0].code})

    assert antwort.status_code == 400


# ---------------------------------------------------------------- Passwort vergessen


def test_reset_setzt_das_passwort_und_beendet_offene_sitzungen(client, mails, post):
    assert client.post(
        "/api/login", json={"email": BESTAND, "password": PASSWORT}
    ).status_code == 200

    client.post("/api/password-reset", json={"email": BESTAND})
    neues = "noch-geheimer-456"
    antwort = client.post(
        "/api/password-reset/confirm",
        json={"email": BESTAND, "code": mails[0].code, "password": neues},
    )

    assert antwort.status_code == 204
    # Das Cookie von vorhin ist entwertet - genau dafuer setzt man zurueck.
    assert client.get("/api/session").json()["authenticated"] is False
    assert client.post(
        "/api/login", json={"email": BESTAND, "password": PASSWORT}
    ).status_code == 401
    assert client.post(
        "/api/login", json={"email": BESTAND, "password": neues}
    ).status_code == 200


def test_reset_fuer_unbekannte_adresse_antwortet_gleich_und_schickt_nichts(
    client, mails
):
    bekannt = client.post("/api/password-reset", json={"email": BESTAND})
    unbekannt = client.post("/api/password-reset", json={"email": "gibts@nicht.de"})

    assert bekannt.status_code == unbekannt.status_code == 202
    assert bekannt.json() == unbekannt.json()
    assert [mail.to for mail in mails] == [BESTAND]


def test_reset_code_taugt_nicht_fuer_die_bestaetigung(client, mails, post):
    """Codes sind an ihren Zweck gebunden - sonst waere ein Reset-Code ein Generalschluessel."""
    _registrieren(client)
    client.post("/api/password-reset", json={"email": NEU})
    reset_code = mails[-1].code

    assert (
        client.post("/api/verify", json={"email": NEU, "code": reset_code}).status_code
        == 400
    )


def test_zu_viele_code_anforderungen_werden_gebremst(client, mails):
    for _ in range(3):
        assert client.post("/api/password-reset", json={"email": BESTAND}).status_code == 202

    gebremst = client.post("/api/password-reset", json={"email": BESTAND})

    assert gebremst.status_code == 429
    assert len(mails) == 3


# ---------------------------------------------------------------- Sitzungen


def test_sitzung_ueberlebt_einen_neustart(client, post):
    client.post("/api/login", json={"email": BESTAND, "password": PASSWORT})
    cookie = client.cookies.get("mailagent_session")

    # Neuer Prozess, neuer Client - das Token steht in der Datenbank, nicht im RAM.
    with TestClient(app, cookies={"mailagent_session": cookie}) as nach_neustart:
        assert nach_neustart.get("/api/session").json()["email"] == BESTAND


def test_abgelaufene_sitzung_gilt_nicht_mehr(client, post):
    client.post("/api/login", json={"email": BESTAND, "password": PASSWORT})
    with post() as db:
        eintrag = db.scalar(select(LoginSession))
        eintrag.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    assert client.get("/api/session").json()["authenticated"] is False


def test_abmelden_entfernt_die_sitzung_aus_der_datenbank(client, post):
    client.post("/api/login", json={"email": BESTAND, "password": PASSWORT})

    client.post("/api/logout")

    with post() as db:
        assert db.scalars(select(LoginSession)).all() == []


def test_reset_entwertet_alte_registrierungscodes(client, mails, post):
    """Ein vorher abgefangener Registrierungscode darf nach dem Passwortwechsel
    keine Sitzung mehr eroeffnen - und ein bestaetigtes Konto braucht /verify nicht."""
    _registrieren(client)
    registrierungscode = mails[0].code
    client.post("/api/verify", json={"email": NEU, "code": registrierungscode})
    client.post("/api/logout")

    client.post("/api/password-reset", json={"email": NEU})
    client.post(
        "/api/password-reset/confirm",
        json={"email": NEU, "code": mails[-1].code, "password": "ganz-neu-und-lang"},
    )
    # Und noch ein frischer Registrierungscode, der nach dem Reset nicht mehr zieht:
    with post() as db:
        nutzer = db.scalar(select(User).where(User.email == NEU))
        assert nutzer.verified_at is not None
        assert db.scalars(select(VerificationCode).where(VerificationCode.user_id == nutzer.id)).all() == []

    assert client.post("/api/verify", json={"email": NEU, "code": registrierungscode}).status_code == 400
    assert client.get("/api/session").json()["authenticated"] is False


def test_reset_antwortet_auch_bei_mailserver_ausfall_gleich(client, monkeypatch):
    """Sonst verriete 503 gegen 202, welche Adressen ein Konto haben."""
    from app import notify

    def kaputt(*args, **kwargs):
        raise notify.MailSendError("SMTP weg")

    monkeypatch.setattr(notify, "send_mail", kaputt)

    bekannt = client.post("/api/password-reset", json={"email": BESTAND})
    unbekannt = client.post("/api/password-reset", json={"email": "gibts@nicht.de"})

    assert bekannt.status_code == unbekannt.status_code == 202
    assert bekannt.json() == unbekannt.json()


def test_anmeldung_ueberholt_keinen_passwortwechsel(client, post, monkeypatch):
    """Passwort geprueft, dann Reset, dann Sitzung anlegen - so ueberlebte das alte
    Passwort den Wechsel. Die Sitzung entsteht nur, wenn der Hash unveraendert ist."""
    auth = importlib.import_module("app.api.auth")
    echt = auth.verify_password

    def wechsel_dazwischen(password, stored):
        ok = echt(password, stored)
        if ok:
            with post() as db:
                accounts.set_password(db, db.scalar(select(User).where(User.email == BESTAND)), "inzwischen-anders")
                db.commit()
        return ok

    monkeypatch.setattr(auth, "verify_password", wechsel_dazwischen)

    antwort = client.post("/api/login", json={"email": BESTAND, "password": PASSWORT})

    assert antwort.status_code == 401
    assert client.get("/api/session").json()["authenticated"] is False


def test_eine_ip_bekommt_nicht_fuer_jede_adresse_ein_neues_versandbudget(client, mails):
    auth = importlib.import_module("app.api.auth")
    auth.code_ip_throttle._events.clear()
    for i in range(20):
        assert client.post("/api/password-reset", json={"email": f"n{i}@example.de"}).status_code == 202

    assert client.post("/api/password-reset", json={"email": "n99@example.de"}).status_code == 429


def test_gleichzeitige_registrierung_derselben_adresse_gibt_keinen_500(client, mails, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    def kollision(*args, **kwargs):
        raise IntegrityError("INSERT users", {}, Exception("unique"))

    monkeypatch.setattr(accounts, "create_tenant_with_owner", kollision)

    antwort = _registrieren(client)

    assert antwort.status_code == 202
    assert mails[-1].to == NEU and "bereits" in mails[-1].body
