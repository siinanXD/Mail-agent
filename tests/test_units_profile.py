"""Wohnungsprofile: Felder, verschluesselte Zugangsdaten und Mandantentrennung."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import crypto
from app.crypto import generate_key
from app.database import repositories as repo
from app.email.importer import import_directory
from app.tenancy import bind_tenant, use_tenant
from tests.fakes import rule_based_extractor
from tests.test_web import NUTZER_1, NUTZER_2, api  # noqa: F401 - api ist eine Fixture


@pytest.fixture
def seeded(session, sample_dir) -> Session:
    import_directory(session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: [])
    session.commit()
    return session


@pytest.fixture
def schluessel(monkeypatch) -> str:
    """ENCRYPTION_KEY fuer die Zugangsdaten - wie in den Postfach-Tests."""
    key = generate_key()
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"encryption_key": key})())
    return key


@pytest.fixture
def profil_api(api, sqlite_engine, monkeypatch):  # noqa: F811 - Fixture aus test_web
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

    monkeypatch.setattr(importlib.import_module("app.api.units"), "tenant_session", tenant_scope)
    return api


def unit_id(client, name: str) -> int:
    return next(u["id"] for u in client.get("/api/units").json()["units"] if u["name"] == name)


# ---------------------------------------------------------------- Datenzugriff


def test_profil_wird_gespeichert_und_gekuerzt(seeded):
    wohnung = next(u for u in repo.list_units(seeded) if u.name == "Haus Anna")

    repo.update_unit_profile(
        seeded,
        wohnung,
        description="  Ruhige Lage am Waldrand.  ",
        house_rules="Nichtraucher, keine Haustiere.",
        rooms=3,
        beds=4,
        size_sqm=78,
        max_guests=5,
        cleaning_window="Abreisetag ab 11:00",
        address="Seeweg 3, 83471 Berchtesgaden",
        floor="EG",
    )
    seeded.commit()

    gespeichert = repo.get_unit(seeded, wohnung.id)
    assert gespeichert.description == "Ruhige Lage am Waldrand."
    assert (gespeichert.rooms, gespeichert.beds, gespeichert.max_guests) == (3, 4, 5)
    assert gespeichert.cleaning_window == "Abreisetag ab 11:00"


def test_profil_eines_fremden_mandanten_ist_unerreichbar(seeded, other_session):
    wohnung = repo.list_units(seeded)[0]

    assert repo.get_unit(other_session, wohnung.id) is None


def test_zustaendige_mitarbeiter_je_wohnung(seeded):
    anna = next(u for u in repo.list_units(seeded) if u.name == "Haus Anna")
    seeblick = next(u for u in repo.list_units(seeded) if u.name == "Ferienwohnung Seeblick")
    repo.create_staff(seeded, name="Maria", phone="+491711111111", unit_ids=[anna.id])
    repo.create_staff(seeded, name="Jonas", phone="+491722222222", unit_ids=[anna.id, seeblick.id])
    inaktiv = repo.create_staff(seeded, name="Ex-Kraft", phone="+491733333333", unit_ids=[anna.id])
    repo.update_staff(seeded, inaktiv, name="Ex-Kraft", phone="+491733333333", unit_ids=[anna.id], active=False)
    seeded.commit()

    zuordnung = repo.staff_by_unit(seeded)

    assert [m.name for m in zuordnung[anna.id]] == ["Jonas", "Maria"]
    assert [m.name for m in zuordnung[seeblick.id]] == ["Jonas"]


# ---------------------------------------------------------------- API


def test_wohnungen_kommen_mit_profil_und_mitarbeitern(profil_api, schluessel):
    client = profil_api(NUTZER_1)
    liste = client.get("/api/units").json()

    assert {u["name"] for u in liste["units"]} == {
        "Ferienwohnung Seeblick",
        "FeWo Bergblick",
        "Haus Anna",
    }
    assert liste["encryption_configured"] is True
    assert all(u["description"] is None for u in liste["units"])


def test_profil_speichern_und_zugang_verschluesseln(profil_api, schluessel, session):
    client = profil_api(NUTZER_1)
    anna = unit_id(client, "Haus Anna")

    antwort = client.put(
        f"/api/units/{anna}",
        json={
            "description": "Ruhige Lage am Waldrand.",
            "house_rules": "Nichtraucher, Ruhe ab 22 Uhr.",
            "rooms": 3,
            "beds": 4,
            "size_sqm": 78,
            "max_guests": 5,
            "cleaning_window": "Abreisetag ab 11:00, fertig bis 15:00",
            "address": "Seeweg 3",
            "floor": "EG",
            "access": "Schlüsselsafe 4711, WLAN: Anna / sommer2026",
        },
    )

    assert antwort.status_code == 200, antwort.text
    gespeichert = antwort.json()
    assert gespeichert["rooms"] == 3
    assert gespeichert["access"] == "Schlüsselsafe 4711, WLAN: Anna / sommer2026"
    assert gespeichert["access_readable"] is True

    # In der Datenbank steht der Zugang nur verschluesselt.
    roh = session.get(type(repo.list_units(session)[0]), anna)
    assert "Schlüsselsafe" not in (roh.access_encrypted or "")
    assert roh.access_encrypted


def test_zugang_ohne_schluessel_wird_abgelehnt(profil_api, monkeypatch):
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"encryption_key": ""})())
    client = profil_api(NUTZER_1)
    anna = unit_id(client, "Haus Anna")

    antwort = client.put(f"/api/units/{anna}", json={"description": "Test", "access": "Code 4711"})

    assert antwort.status_code == 503
    assert "ENCRYPTION_KEY" in antwort.json()["detail"]
    # Nichts halb gespeichert.
    assert client.get("/api/units").json()["units"][0]["description"] is None


def test_leerer_zugang_loescht_den_eintrag(profil_api, schluessel):
    client = profil_api(NUTZER_1)
    anna = unit_id(client, "Haus Anna")
    client.put(f"/api/units/{anna}", json={"access": "Code 4711"})

    antwort = client.put(f"/api/units/{anna}", json={"access": "   "})

    assert antwort.json()["access"] is None


def test_falscher_schluessel_verraet_nichts(profil_api, schluessel, monkeypatch):
    client = profil_api(NUTZER_1)
    anna = unit_id(client, "Haus Anna")
    client.put(f"/api/units/{anna}", json={"access": "Code 4711"})

    anderer = generate_key()
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"encryption_key": anderer})())
    wohnung = next(u for u in client.get("/api/units").json()["units"] if u["id"] == anna)

    assert wohnung["access"] is None
    assert wohnung["access_readable"] is False


def test_fremde_wohnung_liefert_404(profil_api, schluessel):
    erster = profil_api(NUTZER_1)
    anna = unit_id(erster, "Haus Anna")
    zweiter = profil_api(NUTZER_2)

    assert zweiter.get("/api/units").json()["units"] == []
    assert zweiter.put(f"/api/units/{anna}", json={"description": "fremd"}).status_code == 404


def test_unsinnige_werte_werden_abgelehnt(profil_api, schluessel):
    client = profil_api(NUTZER_1)
    anna = unit_id(client, "Haus Anna")

    assert client.put(f"/api/units/{anna}", json={"rooms": -1}).status_code == 422
    assert client.put(f"/api/units/{anna}", json={"floor": "x" * 100}).status_code == 422


def test_wohnungen_brauchen_eine_anmeldung(profil_api):
    anonym = profil_api()

    assert anonym.get("/api/units").status_code == 401
    assert anonym.put("/api/units/1", json={"description": "x"}).status_code == 401


def test_nicht_lesbarer_zugang_wird_beim_speichern_anderer_felder_nicht_geloescht(
    profil_api, schluessel, session, monkeypatch
):
    """Die Oberflaeche zeigt einen nicht entschluesselbaren Zugang als leeres Feld.
    Speichert man dann die Beschreibung, darf der verschluesselte Wert nicht
    verschwinden - "nicht mitgeschickt" heisst "unveraendert"."""
    from sqlalchemy import select

    from app.database.models import Unit

    client = profil_api(NUTZER_1)
    anna = unit_id(client, "Haus Anna")
    client.put(f"/api/units/{anna}", json={"access": "Code 4711"})
    session.expire_all()
    vorher = session.scalar(select(Unit.access_encrypted).where(Unit.id == anna))
    assert vorher

    # Anderer Schluessel: der Wert ist nicht mehr lesbar ...
    anderer = generate_key()
    monkeypatch.setattr(
        crypto, "get_settings", lambda: type("S", (), {"encryption_key": anderer})()
    )
    antwort = client.put(f"/api/units/{anna}", json={"description": "Neu"})

    assert antwort.status_code == 200
    assert antwort.json()["access_readable"] is False
    # ... aber er ist noch da.
    session.expire_all()
    assert session.scalar(select(Unit.access_encrypted).where(Unit.id == anna)) == vorher

    # Ausdruecklich leer geschickt loescht weiterhin.
    client.put(f"/api/units/{anna}", json={"access": ""})
    session.expire_all()
    assert session.scalar(select(Unit.access_encrypted).where(Unit.id == anna)) is None
