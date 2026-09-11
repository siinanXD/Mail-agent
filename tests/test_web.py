"""Verlauf-API, Belege, Anmeldung und Assistent - mit Mandantentrennung."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.database import accounts
from app.database.models import Tenant
from app.email.importer import import_directory, import_email
from app.evidence import Match, collect_evidence, date_variants, find_matches, merge_matches
from app.main import app
from app.tenancy import bind_tenant, get_current_tenant, use_tenant
from tests.fakes import rule_based_extractor

PASSWORT = "sehr-geheim-123"
NUTZER_1 = "anna@standard.de"   # Mandant 1 - hat die Demo-Daten
NUTZER_2 = "bernd@zweiter.de"   # Mandant 2 - leer


@pytest.fixture
def api(session, sqlite_engine, sample_dir, monkeypatch):
    """Demo-Daten in Mandant 1, je ein Nutzer in Mandant 1 und 2.

    Liefert eine Fabrik fuer Clients: ``api()`` anonym, ``api(NUTZER_1)``
    angemeldet. Die gepatchte ``tenant_session`` beachtet den uebergebenen
    Mandanten - ohne das waeren die Trennungstests immer gruen.
    """
    import_directory(
        session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: []
    )
    accounts.create_user(session, tenant_id=1, email=NUTZER_1, password=PASSWORT)
    accounts.create_user(session, tenant_id=2, email=NUTZER_2, password=PASSWORT)
    session.commit()

    make_session = sessionmaker(bind=sqlite_engine, expire_on_commit=False)

    @contextmanager
    def tenant_scope(tenant_id: int | None = None) -> Iterator[Session]:
        db = bind_tenant(make_session(), tenant_id)
        try:
            with use_tenant(tenant_id):
                yield db
            db.commit()
        finally:
            db.close()

    @contextmanager
    def plain_scope() -> Iterator[Session]:
        db = make_session()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    monkeypatch.setattr(importlib.import_module("app.api.timeline"), "tenant_session", tenant_scope)
    monkeypatch.setattr(importlib.import_module("app.api.auth"), "session_scope", plain_scope)

    offene: list[TestClient] = []

    def client_fuer(email: str | None = None) -> TestClient:
        test_client = TestClient(app)
        test_client.__enter__()
        offene.append(test_client)
        if email:
            response = test_client.post(
                "/api/login", json={"email": email, "password": PASSWORT}
            )
            assert response.status_code == 200, response.text
        return test_client

    yield client_fuer
    for test_client in offene:
        test_client.__exit__(None, None, None)


@pytest.fixture
def client(api) -> TestClient:
    """Angemeldet als Nutzer in Mandant 1."""
    return api(NUTZER_1)


def _storno_berger(client: TestClient) -> dict:
    return next(
        e
        for e in client.get("/api/timeline").json()["entries"]
        if e["email_type"] == "cancellation" and e["guest_name"] == "Thomas Berger"
    )


# ---------------------------------------------------------------- Verlauf


def test_verlauf_ist_chronologisch_absteigend(client):
    data = client.get("/api/timeline").json()

    assert data["count"] == 14
    zeitpunkte = [entry["received_at"] for entry in data["entries"]]
    assert zeitpunkte == sorted(zeitpunkte, reverse=True)
    assert data["counts_by_type"]["booking"] == 8
    assert data["counts_by_type"]["cancellation"] == 3
    assert data["counts_by_type"]["change"] == 1


def test_eintraege_tragen_die_verknuepften_daten(client):
    entries = client.get("/api/timeline").json()["entries"]
    storno = _storno_berger(client)

    assert storno["booking_reference"] == "BK-2026-0103"
    assert storno["unit"] == "Ferienwohnung Seeblick"
    assert storno["unit_source"] == "verknuepft"
    assert storno["summary"] == "Grund: Flugausfall"

    aenderung = next(e for e in entries if e["email_type"] == "change")
    assert "2026-09-09" in aenderung["summary"]
    assert "2026-09-10" in aenderung["summary"]


def test_objekt_wird_bei_mails_ohne_datensatz_aus_dem_text_erkannt(client):
    entries = client.get("/api/timeline").json()["entries"]
    anfrage = next(e for e in entries if e["email_type"] == "request")

    assert anfrage["booking_reference"] is None
    assert anfrage["unit"] == "FeWo Bergblick"
    assert anfrage["unit_source"] == "aus dem Text erkannt"


def test_filter_nach_typ_und_suche(client):
    nur_storno = client.get("/api/timeline", params={"types": ["cancellation"]}).json()
    assert nur_storno["count"] == 3
    assert {e["email_type"] for e in nur_storno["entries"]} == {"cancellation"}

    zwei = client.get("/api/timeline", params={"types": ["cancellation", "change"]}).json()
    assert zwei["count"] == 4

    suche = client.get("/api/timeline", params={"search": "Flugausfall"}).json()
    assert suche["count"] == 1
    assert suche["entries"][0]["guest_name"] == "Thomas Berger"


# ---------------------------------------------------------------- Belege


def test_detail_belegt_werte_im_mailtext(client):
    detail = client.get(f"/api/emails/{_storno_berger(client)['email_id']}").json()
    belege = {item["label"]: item for item in detail["evidence"]}

    assert belege["Buchungsnummer"]["value"] == "BK-2026-0103"
    assert belege["Buchungsnummer"]["found"] is True
    assert belege["Objekt"]["found"] is True
    assert belege["Grund"]["value"] == "Flugausfall"
    assert belege["Grund"]["found"] is True
    # Das Stornodatum ist das Empfangsdatum - es steht nicht im Text.
    assert belege["Storniert am"]["found"] is False


def test_hervorhebungen_liegen_im_body_und_treffen_den_text(client):
    detail = client.get(f"/api/emails/{_storno_berger(client)['email_id']}").json()

    body = detail["body"]
    assert detail["highlights"]
    treffer = []
    for stelle in detail["highlights"]:
        assert 0 <= stelle["start"] < stelle["end"] <= len(body)
        treffer.append(body[stelle["start"]:stelle["end"]])

    assert "BK-2026-0103" in treffer
    assert any("Flugausfall" in t for t in treffer)
    enden = [s["end"] for s in detail["highlights"]]
    starts = [s["start"] for s in detail["highlights"]]
    assert all(enden[i] <= starts[i + 1] for i in range(len(starts) - 1))


def test_unbekannte_email_liefert_404(client):
    assert client.get("/api/emails/99999").status_code == 404


def test_gruppenbuchung_zeigt_alle_zimmer_in_verlauf_und_belegen(api, session):
    """Eine Beds24-Mail legt je Zimmer eine Buchung an - angezeigt wurde nur die erste."""
    from tests.test_beds24 import GRUPPE_BUCHUNG

    import_email(session, GRUPPE_BUCHUNG, embedder=lambda c: [])
    session.commit()
    client = api(NUTZER_1)

    eintrag = next(
        e
        for e in client.get("/api/timeline").json()["entries"]
        if e["subject"] == GRUPPE_BUCHUNG.subject
    )
    assert eintrag["booking_reference"] == "87000030, 87000031"
    assert "Zimmer Nr. 1" in eintrag["unit"]
    assert "Zimmer Nr. 3" in eintrag["unit"]

    detail = client.get(f"/api/emails/{eintrag['email_id']}").json()
    nummern = [i["value"] for i in detail["evidence"] if i["field"] == "booking_reference"]
    assert nummern == ["87000030", "87000031"]
    assert detail["booking_reference"] == "87000030, 87000031"
    # Gleicher Gast in beiden Zimmern: ein Beleg, nicht zwei.
    assert len([i for i in detail["evidence"] if i["field"] == "guest_name"]) == 1


def test_mail_ohne_extraktion_hat_keine_belege(client):
    entries = client.get("/api/timeline").json()["entries"]
    beschwerde = next(e for e in entries if e["email_type"] == "complaint")

    detail = client.get(f"/api/emails/{beschwerde['email_id']}").json()

    assert detail["evidence"] == []
    assert detail["highlights"] == []
    assert detail["body"]


# ---------------------------------------------------------------- Mandantentrennung


def test_anderer_mandant_sieht_einen_leeren_verlauf(api):
    zweiter = api(NUTZER_2)
    data = zweiter.get("/api/timeline").json()

    assert data["count"] == 0
    assert data["entries"] == []
    assert data["counts_by_type"] == {}


def test_fremde_mail_liefert_404_statt_403(api):
    """404 verraet nicht, dass es die ID bei einem anderen Mandanten gibt."""
    erster = api(NUTZER_1)
    email_id = erster.get("/api/timeline").json()["entries"][0]["email_id"]
    assert erster.get(f"/api/emails/{email_id}").status_code == 200

    zweiter = api(NUTZER_2)
    assert zweiter.get(f"/api/emails/{email_id}").status_code == 404


# ---------------------------------------------------------------- Beleg-Logik pur


def test_datumsvarianten_decken_gaengige_schreibweisen_ab():
    varianten = date_variants(date(2026, 9, 7))

    assert "2026-09-07" in varianten
    assert "07.09.2026" in varianten
    assert "7.9.2026" in varianten


def test_fundstellen_sind_unabhaengig_von_gross_kleinschreibung():
    treffer = find_matches("Wegen FLUGAUSFALL storniert", "Flugausfall")

    assert len(treffer) == 1
    assert treffer[0].start == 6


def test_ueberlappende_fundstellen_werden_zusammengefasst():
    merged = merge_matches([Match(0, 5), Match(3, 9), Match(20, 25)])

    assert [(m.start, m.end) for m in merged] == [(0, 9), (20, 25)]


def test_abgeleiteter_wert_wird_nicht_als_fund_ausgegeben():
    class FakeBooking:
        booking_reference = "BK-2026-0500"
        guest_name = "Erika Beispiel"
        unit = None
        arrival_date = date(2026, 11, 1)
        departure_date = None

    belege = collect_evidence(
        subject="Buchung",
        body="Hallo, ich buche wie besprochen. Viele Grüße",
        booking=FakeBooking(),
    )
    status = {item.label: item.found for item in belege}

    assert status["Buchungsnummer"] is False
    assert status["Anreise"] is False


# ---------------------------------------------------------------- Anmeldung


def test_ohne_anmeldung_kein_zugriff_auf_mandantendaten(api):
    anonym = api()

    assert anonym.get("/api/session").json()["authenticated"] is False
    assert anonym.get("/api/timeline").status_code == 401
    assert anonym.get("/api/emails/1").status_code == 401
    assert anonym.post("/emails/import", json={}).status_code == 401
    assert anonym.post("/emails/poll").status_code == 401
    assert anonym.get("/reports/cleaning-plan", params={"week": 37}).status_code == 401


def test_sitzung_zeigt_nutzer_und_mandant(client):
    assert client.get("/api/session").json() == {
        "authenticated": True,
        "email": NUTZER_1,
        "tenant_name": "Standard",
    }


def test_falsches_passwort_und_unbekannte_adresse_sind_nicht_unterscheidbar(api):
    anonym = api()
    falsch = anonym.post("/api/login", json={"email": NUTZER_1, "password": "falsch"})
    unbekannt = anonym.post(
        "/api/login", json={"email": "gibts@nicht.de", "password": PASSWORT}
    )

    assert falsch.status_code == unbekannt.status_code == 401
    assert falsch.json() == unbekannt.json()


def test_gross_kleinschreibung_der_adresse_ist_egal(api):
    anonym = api()
    response = anonym.post(
        "/api/login", json={"email": NUTZER_1.upper(), "password": PASSWORT}
    )

    assert response.status_code == 200


def test_abmelden_beendet_die_sitzung(client):
    assert client.get("/api/timeline").status_code == 200

    client.post("/api/logout")

    assert client.get("/api/timeline").status_code == 401


def test_deaktivierter_nutzer_kommt_nicht_rein(api, session):
    nutzer = accounts.get_user_by_email(session, NUTZER_2)
    nutzer.active = False
    session.commit()

    response = api().post("/api/login", json={"email": NUTZER_2, "password": PASSWORT})

    assert response.status_code == 401


def test_zu_viele_fehlversuche_pausieren_die_anmeldung(api, monkeypatch):
    """Ohne Bremse liess sich das Passwort beliebig oft raten."""
    monkeypatch.setattr(importlib.import_module("app.api.auth"), "_failed_logins", {})
    anonym = api()

    for _ in range(5):
        falsch = anonym.post("/api/login", json={"email": NUTZER_2, "password": "falsch"})
        assert falsch.status_code == 401

    # Auch das richtige Passwort hilft jetzt nicht - sonst waere Raten weiter moeglich.
    gesperrt = anonym.post("/api/login", json={"email": NUTZER_2, "password": PASSWORT})
    assert gesperrt.status_code == 429
    # Andere Adressen sind nicht betroffen.
    assert anonym.post("/api/login", json={"email": NUTZER_1, "password": PASSWORT}).status_code == 200


def test_erfolgreiche_anmeldung_setzt_die_fehlversuche_zurueck(api, monkeypatch):
    monkeypatch.setattr(importlib.import_module("app.api.auth"), "_failed_logins", {})
    anonym = api()

    for _ in range(4):
        anonym.post("/api/login", json={"email": NUTZER_2, "password": "falsch"})
    assert anonym.post("/api/login", json={"email": NUTZER_2, "password": PASSWORT}).status_code == 200
    for _ in range(4):
        anonym.post("/api/login", json={"email": NUTZER_2, "password": "falsch"})

    assert anonym.post("/api/login", json={"email": NUTZER_2, "password": PASSWORT}).status_code == 200


def test_deaktivierung_beendet_bestehende_sitzungen(api, session):
    """Frueher galt das Cookie nach der Deaktivierung noch bis zu 12 Stunden."""
    angemeldet = api(NUTZER_1)
    assert angemeldet.get("/api/timeline").status_code == 200

    nutzer = accounts.get_user_by_email(session, NUTZER_1)
    nutzer.active = False
    session.commit()

    assert angemeldet.get("/api/timeline").status_code == 401
    assert angemeldet.get("/api/session").json()["authenticated"] is False


def test_deaktivierter_mandant_beendet_bestehende_sitzungen(api, session):
    angemeldet = api(NUTZER_2)
    assert angemeldet.get("/api/timeline").status_code == 200

    session.get(Tenant, 2).active = False
    session.commit()

    assert angemeldet.get("/api/timeline").status_code == 401


# ---------------------------------------------------------------- Assistent


@pytest.fixture
def fake_agent(monkeypatch):
    """Ersetzt den Agenten - merkt sich Thread, Frage und aktiven Mandanten."""
    from app.agent.agent import AgentAnswer

    gefragt: list[tuple[str, str, int | None]] = []

    class FakeAgent:
        def ask(self, thread_id: str, message: str) -> AgentAnswer:
            gefragt.append((thread_id, message, get_current_tenant()))
            return AgentAnswer(
                answer="Letzte Woche gab es 2 Stornierungen.",
                thread_id=thread_id,
                tool_calls=["count_cancellations"],
            )

    monkeypatch.setattr(
        importlib.import_module("app.api.chat"), "get_agent", lambda: FakeAgent()
    )
    return gefragt


def test_assistent_antwortet_im_mandanten_des_nutzers(client, fake_agent):
    response = client.post(
        "/api/chat", json={"thread_id": "web-abc", "message": "Wie viele Stornierungen?"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["answer"] == "Letzte Woche gab es 2 Stornierungen."
    assert data["tool_calls"] == ["count_cancellations"]
    assert data["thread_id"] == "web-abc"  # ohne Mandanten-Praefix nach aussen
    assert fake_agent == [("tenant-1:web-abc", "Wie viele Stornierungen?", 1)]


def test_gespraechsgedaechtnis_ist_je_mandant_getrennt(api, fake_agent):
    """Gleiche thread_id bei zwei Mandanten darf nicht denselben Verlauf treffen."""
    api(NUTZER_1).post("/api/chat", json={"thread_id": "gleich", "message": "a"})
    api(NUTZER_2).post("/api/chat", json={"thread_id": "gleich", "message": "b"})

    assert [eintrag[0] for eintrag in fake_agent] == ["tenant-1:gleich", "tenant-2:gleich"]
    assert [eintrag[2] for eintrag in fake_agent] == [1, 2]


def test_chat_braucht_eine_anmeldung_auch_auf_dem_alten_pfad(api, fake_agent):
    anonym = api()
    frage = {"thread_id": "t", "message": "hallo"}

    assert anonym.post("/chat", json=frage).status_code == 401
    assert anonym.post("/api/chat", json=frage).status_code == 401
    assert fake_agent == []


def test_gespraech_laesst_sich_zuruecksetzen(client, fake_agent):
    client.post("/api/chat", json={"thread_id": "web-reset", "message": "hallo"})

    assert client.delete("/api/chat/web-reset").status_code == 204


# ---------------------------------------------------------------- Oberflaeche


def test_startseite_ist_ohne_anmeldung_erreichbar(api):
    anonym = api()
    response = anonym.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Mail Agent" in response.text
    assert anonym.get("/static/app.js").status_code == 200


def test_oberflaeche_enthaelt_anmeldung_und_chat_bubble(api):
    seite = api().get("/").text

    assert 'id="email"' in seite
    assert 'id="password"' in seite
    assert 'id="chat-toggle"' in seite
    assert 'id="chat-panel"' in seite
