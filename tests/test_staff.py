"""Mitarbeiter, Wohnungszuordnung und Versand des Putzplans per WhatsApp."""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import repositories as repo
from app.email.importer import import_directory
from app.messaging import whatsapp
from app.messaging.whatsapp import (
    MessageSendError,
    MessagingNotConfiguredError,
    OutgoingMessage,
    SentMessage,
)
from app.reports.cleaning_plan import build_plan
from app.staff import dispatcher
from app.staff.phone import InvalidPhoneError, normalize_phone
from app.staff.plan import (
    CleaningTask,
    cleaning_tasks,
    diff_tasks,
    due_week,
    update_message,
    week_plan,
    weekly_message,
)
from app.tenancy import bind_tenant, use_tenant
from tests.fakes import rule_based_extractor
from tests.test_web import NUTZER_1, NUTZER_2, api  # noqa: F401 - api ist eine Fixture

#: KW 37/2026 = Mo 07.09. bis So 13.09. Faellig: Sa 12.09. Seeblick (Wechsel
#: Meier -> Yilmaz), Sa 12.09. Bergblick (Kowalski ab), So 13.09. Haus Anna (Novak ab).
KW37 = date(2026, 9, 7)
VERSAND = datetime(2026, 9, 6, 18, 0)

MARIA = "+491711111111"
JONAS = "+491722222222"


class FakeSender:
    """Statt Twilio: merkt sich jede Nachricht, scheitert fuer ausgewaehlte Nummern."""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[OutgoingMessage] = []
        self.fail_for = fail_for or set()

    def __call__(self, message: OutgoingMessage) -> SentMessage:
        if message.to in self.fail_for:
            raise MessageSendError("Twilio 400 (Code 21211): Invalid 'To' Phone Number")
        self.sent.append(message)
        return SentMessage(provider_message_id=f"SM{len(self.sent)}")


@pytest.fixture
def seeded(session, sample_dir) -> Session:
    import_directory(session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: [])
    session.commit()
    return session


@pytest.fixture
def sessions(monkeypatch, seeded, other_session) -> Session:
    """Der Versand oeffnet eigene Sessions - hier die Test-Sessions je Mandant."""
    by_tenant = {1: seeded, 2: other_session}

    @contextmanager
    def scope(tenant_id: int | None = None) -> Iterator[Session]:
        db = by_tenant[tenant_id]
        with use_tenant(tenant_id):
            yield db
        db.commit()

    @contextmanager
    def plain() -> Iterator[Session]:
        yield seeded

    monkeypatch.setattr(dispatcher, "tenant_session", scope)
    monkeypatch.setattr(dispatcher, "session_scope", plain)
    return seeded


def unit_id(session: Session, name: str) -> int:
    return next(unit.id for unit in repo.list_units(session) if unit.name == name)


def add_staff(session: Session, name: str, phone: str, *units: str):
    member = repo.create_staff(
        session, name=name, phone=phone, unit_ids=[unit_id(session, unit) for unit in units]
    )
    session.commit()
    return member


def cancel(session: Session, guest_name: str) -> None:
    (booking,) = repo.search_bookings(session, guest_name=guest_name)
    booking.status = "cancelled"
    session.commit()


# ---------------------------------------------------------------- Telefonnummern


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0171 1234567", "+491711234567"),
        ("0171/123 45-67", "+491711234567"),
        ("+49 (0)171 1234567", "+491711234567"),
        ("0049 171 1234567", "+491711234567"),
        ("+43 660 1234567", "+436601234567"),
    ],
)
def test_telefonnummern_werden_auf_e164_gebracht(raw, expected):
    assert normalize_phone(raw, default_country_code="49") == expected


@pytest.mark.parametrize("raw", ["", "keine nummer", "171 1234567", "+49 12", "0171 12345678901234"])
def test_unbrauchbare_telefonnummern_werden_abgelehnt(raw):
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw, default_country_code="49")


# ---------------------------------------------------------------- Plan


def test_reinigungen_stimmen_mit_dem_excel_putzplan_ueberein(seeded):
    tasks = cleaning_tasks(seeded, start=KW37, end=date(2026, 9, 13))
    excel = build_plan(seeded, year=2026, week=37)

    assert {(task.unit_name, task.day) for task in tasks} == {
        (row.unit_name, day) for row in excel.rows for day in row.cleaning_days
    }
    assert {task.unit_name for task in tasks if task.turnover} == {"Ferienwohnung Seeblick"}


def test_plan_verteilt_reinigungen_nach_wohnungen(seeded):
    add_staff(seeded, "Maria", MARIA, "Ferienwohnung Seeblick", "Haus Anna")
    add_staff(seeded, "Jonas", JONAS, "Haus Anna")

    plan = week_plan(seeded, KW37)
    by_name = {staff.name: staff for staff in plan.staff}

    assert [(t.day, t.unit_name) for t in by_name["Maria"].tasks] == [
        (date(2026, 9, 12), "Ferienwohnung Seeblick"),
        (date(2026, 9, 13), "Haus Anna"),
    ]
    # Zwei Mitarbeiter fuer dieselbe Wohnung: die Reinigung steht bei beiden.
    assert [t.unit_name for t in by_name["Jonas"].tasks] == ["Haus Anna"]
    assert [t.unit_name for t in plan.unassigned] == ["FeWo Bergblick"]


def test_inaktive_mitarbeiter_bekommen_keinen_plan(seeded):
    member = add_staff(seeded, "Maria", MARIA, "Haus Anna")
    repo.update_staff(
        seeded, member, name="Maria", phone=MARIA, unit_ids=[unit_id(seeded, "Haus Anna")], active=False
    )
    seeded.commit()

    plan = week_plan(seeded, KW37)

    assert plan.staff == []
    assert "Haus Anna" in [task.unit_name for task in plan.unassigned]


def test_wohnungen_lassen_sich_umhaengen(seeded):
    """Eine Wohnung bleibt, eine faellt weg, eine kommt dazu - ohne Unique-Verletzung."""
    member = add_staff(seeded, "Maria", MARIA, "Haus Anna", "FeWo Bergblick")

    repo.update_staff(
        seeded,
        member,
        name="Maria",
        phone=MARIA,
        active=True,
        unit_ids=[unit_id(seeded, "FeWo Bergblick"), unit_id(seeded, "Ferienwohnung Seeblick")],
    )
    seeded.commit()
    seeded.expire_all()

    reloaded = repo.get_staff(seeded, member.id)
    assert sorted(a.unit.name for a in reloaded.assignments) == ["FeWo Bergblick", "Ferienwohnung Seeblick"]


def test_mitarbeiter_und_wohnungen_sind_je_mandant_getrennt(seeded, other_session):
    member = add_staff(seeded, "Maria", MARIA, "Haus Anna")

    assert repo.list_staff(other_session) == []
    assert repo.get_staff(other_session, member.id) is None
    with pytest.raises(repo.UnknownUnitError):
        repo.create_staff(
            other_session, name="Fremd", phone=JONAS, unit_ids=[unit_id(seeded, "Haus Anna")]
        )
    other_session.rollback()


# ---------------------------------------------------------------- Nachrichten


def test_nachricht_nennt_tage_und_wohnungen_aber_keine_gaeste(seeded):
    add_staff(seeded, "Maria", MARIA, "Ferienwohnung Seeblick", "Haus Anna")
    tasks = week_plan(seeded, KW37).staff[0].tasks

    body, variables = weekly_message(
        tenant_name="Standard", staff_name="Maria", week_start=KW37, tasks=tasks
    )

    assert "Putzplan KW 37 (07.09.–13.09.2026) von Standard – 2 Reinigungen" in body
    assert "Sa 12.09. – Ferienwohnung Seeblick (Wechsel: neuer Gast reist am selben Tag an)" in body
    assert "So 13.09. – Haus Anna" in body
    for gast in ("Meier", "Yilmaz", "Novak"):
        assert gast not in body
    assert variables["1"] == "Maria"
    # WhatsApp lehnt Zeilenumbrueche in Vorlagenvariablen ab.
    assert all("\n" not in value for value in variables.values())
    assert "So 13.09. – Haus Anna" in variables["3"]


def test_leere_woche_sagt_das_ausdruecklich():
    body, variables = weekly_message(
        tenant_name="Standard", staff_name="Maria", week_start=KW37, tasks=[]
    )

    assert "keine Reinigungen" in body
    assert variables["3"] == "keine Reinigungen"


def test_aenderungen_werden_als_neu_entfaellt_und_geaendert_erkannt():
    seeblick = CleaningTask(date(2026, 9, 12), 1, "Seeblick", turnover=True)
    anna = CleaningTask(date(2026, 9, 13), 2, "Anna")
    bergblick = CleaningTask(date(2026, 9, 10), 3, "Bergblick")
    seeblick_ohne_wechsel = CleaningTask(date(2026, 9, 12), 1, "Seeblick")

    diff = diff_tasks([seeblick, anna], [seeblick_ohne_wechsel, bergblick])

    assert diff.added == [bergblick]
    assert diff.removed == [anna]
    assert diff.changed == [seeblick_ohne_wechsel]

    body, variables = update_message(
        tenant_name="Standard",
        staff_name="Maria",
        week_start=KW37,
        diff=diff,
        tasks=[bergblick, seeblick_ohne_wechsel],
    )
    assert "Neu: Do 10.09. – Bergblick" in body
    assert "Entfällt: So 13.09. – Anna" in body
    assert "Geändert: Sa 12.09. – Seeblick (kein Wechsel mehr)" in body
    assert "\n" not in variables["3"]


# ---------------------------------------------------------------- Versandtermin

SONNTAG_18_UHR = {"weekday": 6, "send_time": time(18, 0)}
EINGESCHALTET = datetime(2026, 9, 1, 9, 0)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # Termin So 06.09. ist erreicht, seine Woche (KW 37) laeuft noch.
        (datetime(2026, 9, 13, 17, 59), date(2026, 9, 7)),
        # Neuer Termin -> die kommende Woche
        (datetime(2026, 9, 13, 18, 0), date(2026, 9, 14)),
        # Server war Sonntagabend aus -> wird nachgeholt, solange die Woche laeuft
        (datetime(2026, 9, 14, 10, 0), date(2026, 9, 14)),
        (datetime(2026, 9, 20, 12, 0), date(2026, 9, 14)),
    ],
)
def test_faellige_woche_bei_versand_am_sonntag(now, expected):
    assert due_week(now, active_since=EINGESCHALTET, **SONNTAG_18_UHR) == expected


def test_montags_geht_die_laufende_woche_raus():
    assert (
        due_week(datetime(2026, 9, 14, 7, 0), weekday=0, send_time=time(7, 0), active_since=EINGESCHALTET)
        == date(2026, 9, 14)
    )


def test_termine_vor_dem_einschalten_werden_nicht_nachgeholt():
    """Einschalten am Mittwoch darf nicht sofort den Plan der laufenden Woche schicken."""
    eingeschaltet = datetime(2026, 9, 9, 10, 0)

    assert due_week(datetime(2026, 9, 9, 10, 5), active_since=eingeschaltet, **SONNTAG_18_UHR) is None
    assert (
        due_week(datetime(2026, 9, 13, 18, 0), active_since=eingeschaltet, **SONNTAG_18_UHR)
        == date(2026, 9, 14)
    )


# ---------------------------------------------------------------- Versand


def test_zeitplan_verschickt_erst_zum_termin_und_nur_einmal(sessions):
    add_staff(sessions, "Maria", MARIA, "Haus Anna")
    repo.save_cleaning_schedule(
        sessions, enabled=True, weekday=6, send_time=time(18, 0), now=EINGESCHALTET
    )
    sessions.commit()
    sender = FakeSender()

    assert dispatcher.run_due(now=datetime(2026, 9, 6, 17, 59), sender=sender) == []

    outcomes = dispatcher.run_due(now=datetime(2026, 9, 6, 18, 3), sender=sender)
    assert [(o.name, o.success, o.task_count) for o in outcomes] == [("Maria", True, 1)]
    assert sender.sent[0].to == MARIA
    assert "KW 37" in sender.sent[0].body

    # Naechster Durchlauf, Neustart - egal: verschickt ist verschickt.
    assert dispatcher.run_due(now=datetime(2026, 9, 6, 18, 8), sender=sender) == []
    assert len(sender.sent) == 1


def test_ohne_eingeschalteten_zeitplan_geht_nichts_raus(sessions):
    add_staff(sessions, "Maria", MARIA, "Haus Anna")
    repo.save_cleaning_schedule(
        sessions, enabled=False, weekday=6, send_time=time(18, 0), now=EINGESCHALTET
    )
    sessions.commit()

    assert dispatcher.run_due(now=datetime(2026, 9, 6, 18, 3), sender=FakeSender()) == []


def test_wochenplan_geht_je_mitarbeiter_genau_einmal_raus(seeded):
    add_staff(seeded, "Maria", MARIA, "Ferienwohnung Seeblick")
    add_staff(seeded, "Jonas", JONAS, "Haus Anna")
    sender = FakeSender()

    first = dispatcher.send_week(seeded, week_start=KW37, now=VERSAND, sender=sender, only_missing=True)
    again = dispatcher.send_week(seeded, week_start=KW37, now=VERSAND, sender=sender, only_missing=True)

    assert sorted(outcome.name for outcome in first) == ["Jonas", "Maria"]
    assert again == []
    assert sorted(message.to for message in sender.sent) == [MARIA, JONAS]


def test_fehlgeschlagener_versand_wird_nur_begrenzt_wiederholt(seeded):
    add_staff(seeded, "Maria", MARIA, "Haus Anna")
    sender = FakeSender(fail_for={MARIA})

    for _ in range(dispatcher.MAX_ATTEMPTS + 2):
        dispatcher.send_week(seeded, week_start=KW37, now=VERSAND, sender=sender, only_missing=True)

    versuche = repo.dispatches_for_week(seeded, KW37)
    assert len(versuche) == dispatcher.MAX_ATTEMPTS
    assert not any(versuch.success for versuch in versuche)
    assert "21211" in versuche[0].error


def test_mitten_in_der_woche_nur_noch_kommende_reinigungen(seeded):
    add_staff(seeded, "Maria", MARIA, "Ferienwohnung Seeblick", "Haus Anna")
    sender = FakeSender()

    dispatcher.send_week(seeded, week_start=KW37, now=datetime(2026, 9, 13, 8, 0), sender=sender)

    assert "Seeblick" not in sender.sent[0].body
    assert "So 13.09. – Haus Anna" in sender.sent[0].body


# ---------------------------------------------------------------- Aenderungen nach dem Versand


def test_storno_nach_versand_geht_nur_an_betroffene(sessions):
    add_staff(sessions, "Maria", MARIA, "FeWo Bergblick")
    add_staff(sessions, "Jonas", JONAS, "Haus Anna")
    sender = FakeSender()
    dispatcher.send_week(sessions, week_start=KW37, now=VERSAND, sender=sender)
    sender.sent.clear()
    nach_dem_abruf = datetime(2026, 9, 8, 12, 0)

    assert dispatcher.notify_changes(1, now=nach_dem_abruf, sender=sender) == []

    cancel(sessions, "Kowalski")
    outcomes = dispatcher.notify_changes(1, now=nach_dem_abruf, sender=sender)

    assert [(o.name, o.kind) for o in outcomes] == [("Maria", "update")]
    (nachricht,) = sender.sent
    assert nachricht.to == MARIA
    assert nachricht.template == "update"
    assert "Entfällt: Sa 12.09. – FeWo Bergblick" in nachricht.body
    # Der neue Stand gilt jetzt als verschickt - kein zweites Update.
    assert dispatcher.notify_changes(1, now=nach_dem_abruf, sender=sender) == []


def test_aenderungen_an_vergangenen_tagen_loesen_nichts_aus(sessions):
    add_staff(sessions, "Maria", MARIA, "FeWo Bergblick")
    sender = FakeSender()
    dispatcher.send_week(sessions, week_start=KW37, now=VERSAND, sender=sender)
    sender.sent.clear()

    cancel(sessions, "Kowalski")  # Reinigung war Sa 12.09.

    assert dispatcher.notify_changes(1, now=datetime(2026, 9, 13, 9, 0), sender=sender) == []


def test_ohne_whatsapp_zugang_passiert_nach_dem_abruf_nichts(monkeypatch):
    monkeypatch.setattr(whatsapp, "whatsapp_configured", lambda settings=None: False)
    monkeypatch.setattr(
        dispatcher, "tenant_session", lambda *a, **k: pytest.fail("keine Datenbank noetig")
    )

    assert dispatcher.notify_changes(1) == []


def test_versand_laesst_sich_je_instanz_abschalten(monkeypatch):
    """Wie WATCH_ENABLED: Bei mehreren Instanzen verschickt nur eine."""
    monkeypatch.setattr(
        dispatcher, "get_settings", lambda: Settings(_env_file=None, cleaning_dispatch_enabled=False)
    )
    monkeypatch.setattr(whatsapp, "whatsapp_configured", lambda settings=None: True)
    tasks: list = []

    dispatcher.start(tasks)

    assert tasks == []


# ---------------------------------------------------------------- Twilio


def _settings(**overrides) -> Settings:
    values = {
        "twilio_account_sid": "AC123",
        "twilio_auth_token": "geheim",
        "twilio_whatsapp_from": "+14155238886",
        "twilio_content_sid": "",
        "twilio_update_content_sid": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _twilio(status: int = 201, payload: dict | None = None):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=payload or {"sid": "SM42", "status": "queued"})

    return requests, httpx.Client(transport=httpx.MockTransport(handler))


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


def test_twilio_bekommt_freien_text_ohne_vorlage():
    requests, client = _twilio()

    sent = whatsapp.send_whatsapp(
        OutgoingMessage(to=MARIA, body="Zeile 1\nZeile 2", variables={"1": "Maria"}),
        settings=_settings(),
        client=client,
    )

    assert sent.provider_message_id == "SM42"
    (request,) = requests
    assert request.url.path == "/2010-04-01/Accounts/AC123/Messages.json"
    assert request.headers["authorization"].startswith("Basic ")
    assert _form(request) == {
        "From": "whatsapp:+14155238886",
        "To": f"whatsapp:{MARIA}",
        "Body": "Zeile 1\nZeile 2",
    }


def test_twilio_nutzt_die_freigegebene_vorlage():
    requests, client = _twilio()

    whatsapp.send_whatsapp(
        OutgoingMessage(
            to=MARIA,
            body="egal",
            variables={"1": "Maria", "3": "Sa 12.09. – Haus Anna"},
            template="update",
        ),
        settings=_settings(twilio_content_sid="HXplan", twilio_update_content_sid="HXupdate"),
        client=client,
    )

    form = _form(requests[0])
    assert form["ContentSid"] == "HXupdate"
    assert json.loads(form["ContentVariables"]) == {"1": "Maria", "3": "Sa 12.09. – Haus Anna"}
    assert "Body" not in form


def test_twilio_fehler_wird_mit_code_gemeldet():
    _, client = _twilio(status=400, payload={"code": 21211, "message": "Invalid 'To' Phone Number"})

    with pytest.raises(MessageSendError, match="21211"):
        whatsapp.send_whatsapp(OutgoingMessage(to=MARIA, body="x"), settings=_settings(), client=client)


def test_ohne_zugangsdaten_wird_nichts_gesendet():
    with pytest.raises(MessagingNotConfiguredError):
        whatsapp.send_whatsapp(
            OutgoingMessage(to=MARIA, body="x"), settings=_settings(twilio_auth_token="")
        )


# ---------------------------------------------------------------- API


@pytest.fixture
def staff_api(api, sqlite_engine, monkeypatch):  # noqa: F811 - Fixture aus test_web
    """Wie ``api`` aus test_web, dazu Mitarbeiter-API und Versand auf der Test-DB."""
    make_session = sessionmaker(bind=sqlite_engine, expire_on_commit=False)

    @contextmanager
    def tenant_scope(tenant_id: int | None = None) -> Iterator[Session]:
        db = bind_tenant(make_session(), tenant_id)
        try:
            with use_tenant(tenant_id):
                yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    for module in ("app.api.staff", "app.staff.dispatcher"):
        monkeypatch.setattr(importlib.import_module(module), "tenant_session", tenant_scope)
    monkeypatch.setattr(dispatcher, "current_time", lambda: VERSAND)
    return api


@pytest.fixture
def fake_whatsapp(monkeypatch) -> FakeSender:
    sender = FakeSender()
    monkeypatch.setattr(whatsapp, "whatsapp_configured", lambda settings=None: True)
    monkeypatch.setattr(whatsapp, "send_whatsapp", sender)
    return sender


def _units(client) -> dict[str, int]:
    return {unit["name"]: unit["id"] for unit in client.get("/api/staff").json()["units"]}


def test_mitarbeiter_mit_wohnungen_anlegen_und_vorschau(staff_api):
    client = staff_api(NUTZER_1)
    units = _units(client)
    assert set(units) == {"Ferienwohnung Seeblick", "FeWo Bergblick", "Haus Anna"}

    response = client.post(
        "/api/staff",
        json={
            "name": "  Maria   Muster ",
            "phone": "0171 1111111",
            "unit_ids": [units["Haus Anna"], units["FeWo Bergblick"]],
        },
    )
    assert response.status_code == 201, response.text
    maria = response.json()
    assert maria["name"] == "Maria Muster"
    assert maria["phone"] == MARIA
    assert [unit["name"] for unit in maria["units"]] == ["FeWo Bergblick", "Haus Anna"]

    preview = client.get("/api/cleaning-schedule/preview", params={"week_start": "2026-09-07"}).json()
    assert preview["label"] == "KW 37 (07.09.–13.09.2026)"
    (plan,) = preview["staff"]
    assert [(task["day"], task["unit"]) for task in plan["tasks"]] == [
        ("2026-09-12", "FeWo Bergblick"),
        ("2026-09-13", "Haus Anna"),
    ]
    assert plan["last_dispatch"] is None
    assert [task["unit"] for task in preview["unassigned"]] == ["Ferienwohnung Seeblick"]


def test_wohnungen_im_dropdown_umhaengen_und_mitarbeiter_loeschen(staff_api):
    client = staff_api(NUTZER_1)
    units = _units(client)
    maria = client.post(
        "/api/staff", json={"name": "Maria", "phone": MARIA, "unit_ids": [units["Haus Anna"]]}
    ).json()

    changed = client.put(
        f"/api/staff/{maria['id']}",
        json={"name": "Maria", "phone": MARIA, "unit_ids": [units["Ferienwohnung Seeblick"]], "active": True},
    )

    assert changed.status_code == 200, changed.text
    assert [unit["name"] for unit in changed.json()["units"]] == ["Ferienwohnung Seeblick"]
    assert client.delete(f"/api/staff/{maria['id']}").status_code == 204
    assert client.get("/api/staff").json()["staff"] == []


def test_ungueltige_eingaben_werden_abgelehnt(staff_api):
    client = staff_api(NUTZER_1)

    assert client.post("/api/staff", json={"name": "Maria", "phone": "keine nummer"}).status_code == 400
    assert client.post("/api/staff", json={"name": "   ", "phone": "0171 1111111"}).status_code == 400
    assert (
        client.post("/api/staff", json={"name": "Maria", "phone": "0171 1111111", "unit_ids": [99999]}).status_code
        == 400
    )
    assert client.get("/api/staff").json()["staff"] == []  # nichts halb angelegt
    assert (
        client.get("/api/cleaning-schedule/preview", params={"week_start": "2026-09-08"}).status_code
        == 400
    )


def test_fremde_mitarbeiter_und_wohnungen_sind_tabu(staff_api, fake_whatsapp):
    erster = staff_api(NUTZER_1)
    units = _units(erster)
    maria = erster.post(
        "/api/staff", json={"name": "Maria", "phone": MARIA, "unit_ids": [units["Haus Anna"]]}
    ).json()

    zweiter = staff_api(NUTZER_2)
    fremd = {"name": "Fremd", "phone": JONAS}

    assert zweiter.get("/api/staff").json() == {"staff": [], "units": []}
    assert zweiter.put(f"/api/staff/{maria['id']}", json=fremd).status_code == 404
    assert zweiter.delete(f"/api/staff/{maria['id']}").status_code == 404
    assert zweiter.post("/api/staff", json={**fremd, "unit_ids": [units["Haus Anna"]]}).status_code == 400
    assert (
        zweiter.post("/api/cleaning-schedule/send", json={"staff_id": maria["id"]}).status_code == 404
    )
    assert fake_whatsapp.sent == []


def test_zeitplan_speichern_zeigt_den_naechsten_termin(staff_api):
    client = staff_api(NUTZER_1)

    leer = client.get("/api/cleaning-schedule").json()
    assert leer["enabled"] is False
    assert leer["default_week_start"] == "2026-09-07"

    saved = client.put(
        "/api/cleaning-schedule", json={"enabled": True, "weekday": 6, "send_time": "19:30"}
    ).json()
    assert saved["enabled"] is True
    assert saved["send_time"] == "19:30"
    assert saved["next_send_at"] == "2026-09-06T19:30:00"
    assert saved["next_week_start"] == "2026-09-07"
    assert [week["week_start"] for week in saved["weeks"]] == ["2026-08-31", "2026-09-07"]

    assert (
        client.put("/api/cleaning-schedule", json={"enabled": True, "weekday": 7, "send_time": "19:30"}).status_code
        == 422
    )
    assert staff_api(NUTZER_2).get("/api/cleaning-schedule").json()["enabled"] is False


def test_jetzt_senden_aus_der_oberflaeche(staff_api, fake_whatsapp):
    client = staff_api(NUTZER_1)
    units = _units(client)
    client.post("/api/staff", json={"name": "Maria", "phone": MARIA, "unit_ids": [units["Haus Anna"]]})
    client.post("/api/staff", json={"name": "Jonas", "phone": JONAS, "unit_ids": [units["FeWo Bergblick"]]})

    result = client.post("/api/cleaning-schedule/send", json={"week_start": "2026-09-07"})

    assert result.status_code == 200, result.text
    assert (result.json()["sent"], result.json()["failed"]) == (2, 0)
    assert sorted(message.to for message in fake_whatsapp.sent) == [MARIA, JONAS]
    preview = client.get("/api/cleaning-schedule/preview", params={"week_start": "2026-09-07"}).json()
    assert all(plan["last_dispatch"]["success"] for plan in preview["staff"])
    assert not any(plan["changed_since_dispatch"] for plan in preview["staff"])


def test_senden_ohne_whatsapp_zugang_meldet_503(staff_api, monkeypatch):
    monkeypatch.setattr(whatsapp, "whatsapp_configured", lambda settings=None: False)
    client = staff_api(NUTZER_1)

    response = client.post("/api/cleaning-schedule/send", json={})

    assert response.status_code == 503
    assert "TWILIO" in response.json()["detail"]


def test_ohne_anmeldung_keine_mitarbeiterdaten(staff_api):
    anonym = staff_api()

    assert anonym.get("/api/staff").status_code == 401
    assert anonym.post("/api/staff", json={"name": "x", "phone": MARIA}).status_code == 401
    assert anonym.delete("/api/staff/1").status_code == 401
    assert anonym.get("/api/cleaning-schedule").status_code == 401
    assert anonym.get("/api/cleaning-schedule/preview").status_code == 401
    assert anonym.post("/api/cleaning-schedule/send", json={}).status_code == 401


# ---------------------------------------------------------------- Postgres


def test_rls_trennt_mitarbeiter_und_versand_in_der_datenbank(pg_engine):
    factory = sessionmaker(bind=pg_engine)
    with bind_tenant(factory(), 1) as eins:
        unit = repo.get_or_create_unit(eins, "Haus Anna")
        member = repo.create_staff(eins, name="Maria", phone=MARIA, unit_ids=[unit.id])
        repo.save_cleaning_schedule(
            eins, enabled=True, weekday=6, send_time=time(18, 0), now=EINGESCHALTET
        )
        repo.add_dispatch(
            eins,
            staff_id=member.id,
            week_start=KW37,
            kind="plan",
            success=True,
            tasks=[CleaningTask(date(2026, 9, 13), unit.id, "Haus Anna").to_json()],
            created_at=VERSAND,
        )
        eins.commit()
        assert repo.dispatched_weeks(eins, since=KW37) == [KW37]
        assert repo.get_cleaning_schedule(eins).send_time == time(18, 0)

    with bind_tenant(factory(), 2) as zwei:
        for table in ("staff_members", "staff_units", "cleaning_schedules", "cleaning_dispatches"):
            assert zwei.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0

    with pytest.raises(DBAPIError, match="row-level security"):
        with pg_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', '2', true)"))
            conn.execute(
                text(
                    "INSERT INTO staff_members (tenant_id, name, phone, active, created_at) "
                    "VALUES (1, 'x', '+491700000000', true, now())"
                )
            )
