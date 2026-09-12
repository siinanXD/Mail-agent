"""Belegungskalender: Monatsraster, Tagesfarben und Buchungsdetail."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.database import repositories as repo
from app.email.importer import import_directory
from app.reports.calendar import BELEGT, FREI, STORNIERT, build_month, grid_range, month_range
from app.tenancy import bind_tenant, use_tenant
from tests.fakes import rule_based_extractor
from tests.test_web import NUTZER_1, NUTZER_2, api  # noqa: F401 - api ist eine Fixture

#: Demo-Daten liegen im September 2026.
JAHR, MONAT = 2026, 9


@pytest.fixture
def seeded(session, sample_dir) -> Session:
    import_directory(session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: [])
    session.commit()
    return session


def tag(kalender, iso: str):
    gesucht = date.fromisoformat(iso)
    return next(day for week in kalender.weeks for day in week if day.day == gesucht)


def storniere(session: Session, guest_name: str) -> None:
    (booking,) = repo.search_bookings(session, guest_name=guest_name)
    booking.status = "cancelled"
    session.commit()


# ---------------------------------------------------------------- Raster


def test_monatsgrenzen_und_raster():
    erster, letzter = month_range(2026, 9)
    assert (erster, letzter) == (date(2026, 9, 1), date(2026, 9, 30))

    # 01.09.2026 ist ein Dienstag, 30.09. ein Mittwoch -> Raster Mo 31.08. bis So 04.10.
    start, ende = grid_range(erster, letzter)
    assert (start, ende) == (date(2026, 8, 31), date(2026, 10, 4))
    assert start.weekday() == 0 and ende.weekday() == 6


def test_ungueltiger_monat_wird_abgelehnt():
    with pytest.raises(ValueError):
        month_range(2026, 13)


def test_raster_hat_volle_wochen_und_markiert_randtage(seeded):
    kalender = build_month(seeded, year=JAHR, month=MONAT)

    assert all(len(woche) == 7 for woche in kalender.weeks)
    assert [tag.day for tag in kalender.weeks[0]][0] == date(2026, 8, 31)
    assert tag(kalender, "2026-08-31").in_month is False
    assert tag(kalender, "2026-09-01").in_month is True
    assert len(kalender.days) == 30


# ---------------------------------------------------------------- Tagesfarben


def test_belegte_tage_zeigen_ihre_wohnungen(seeded):
    kalender = build_month(seeded, year=JAHR, month=MONAT)

    achter = tag(kalender, "2026-09-08")
    assert achter.status == BELEGT
    assert "Ferienwohnung Seeblick" in {b.unit_name for b in achter.active}


def test_wechseltag_zeigt_abreise_und_anreise(seeded):
    """Sa 12.09.: Familie Meier reist ab, Emre Yilmaz zieht am selben Tag ein."""
    kalender = build_month(seeded, year=JAHR, month=MONAT)

    zwoelfter = tag(kalender, "2026-09-12")
    seeblick = [b for b in zwoelfter.bookings if b.unit_name == "Ferienwohnung Seeblick"]

    assert any(b.departure and b.guest_name == "Familie Meier" for b in seeblick)
    assert any(b.arrival and b.guest_name == "Emre Yilmaz" for b in seeblick)
    assert zwoelfter.status == BELEGT


def test_abreisetag_allein_macht_den_tag_nicht_belegt(other_session):
    """Eine Nacht zaehlt bis vor der Abreise - sonst waere der Tag doppelt vergeben.

    Eigener, leerer Mandant: Der Tagesstatus gilt fuer alle Wohnungen zusammen,
    mit den Demo-Daten waeren am selben Tag immer noch andere Objekte belegt.
    """
    wohnung = repo.get_or_create_unit(other_session, "Testwohnung")
    repo.upsert_booking(
        other_session,
        booking_reference="T-1",
        guest_name="Testgast",
        arrival_date=date(2026, 9, 10),
        departure_date=date(2026, 9, 12),
        unit_id=wohnung.id,
    )
    other_session.commit()

    kalender = build_month(other_session, year=JAHR, month=MONAT)

    assert tag(kalender, "2026-09-10").status == BELEGT
    assert tag(kalender, "2026-09-11").status == BELEGT
    abreise = tag(kalender, "2026-09-12")
    assert abreise.status == FREI
    assert abreise.occupied == []
    assert [b.departure for b in abreise.bookings] == [True]


def test_stornierte_buchung_faerbt_den_tag_rot_statt_gruen(other_session):
    """Storniert heisst: Tag ist wieder frei, aber man sieht, dass da etwas war."""
    wohnung = repo.get_or_create_unit(other_session, "Testwohnung")
    repo.upsert_booking(
        other_session,
        booking_reference="T-2",
        guest_name="Storno-Gast",
        arrival_date=date(2026, 9, 20),
        departure_date=date(2026, 9, 22),
        unit_id=wohnung.id,
    )
    other_session.commit()
    assert tag(build_month(other_session, year=JAHR, month=MONAT), "2026-09-20").status == BELEGT

    storniere(other_session, "Storno-Gast")
    kalender = build_month(other_session, year=JAHR, month=MONAT)

    zwanzigster = tag(kalender, "2026-09-20")
    assert zwanzigster.status == STORNIERT
    assert zwanzigster.occupied == []
    assert [b.guest_name for b in zwanzigster.cancelled] == ["Storno-Gast"]
    # Der Abreisetag war ohnehin frei - er wird nicht rot.
    assert tag(kalender, "2026-09-22").status == FREI


def test_umbuchung_wird_am_tag_markiert(seeded):
    """Novak wurde von Mi 09.09. auf Do 10.09. umgebucht."""
    kalender = build_month(seeded, year=JAHR, month=MONAT)

    zehnter = tag(kalender, "2026-09-10")
    assert zehnter.changed is True
    assert any(b.changed and b.guest_name == "Marta Novak" for b in zehnter.bookings)


def test_leerer_monat_ist_komplett_frei(seeded):
    kalender = build_month(seeded, year=2026, month=1)

    assert kalender.occupied_days == 0
    assert {tag.status for tag in kalender.days} == {FREI}


# ---------------------------------------------------------------- API


@pytest.fixture
def kalender_api(api, sqlite_engine, monkeypatch):  # noqa: F811 - Fixture aus test_web
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

    monkeypatch.setattr(importlib.import_module("app.api.calendar"), "tenant_session", tenant_scope)
    return api


def test_kalender_liefert_den_monat_mit_navigation(kalender_api):
    client = kalender_api(NUTZER_1)

    data = client.get("/api/calendar", params={"year": 2026, "month": 9}).json()

    assert data["label"] == "September 2026"
    assert data["first_day"] == "2026-09-01"
    assert data["previous"] == {"year": 2026, "month": 8}
    assert data["next"] == {"year": 2026, "month": 10}
    assert len(data["weeks"][0]) == 7
    tage = {d["date"]: d for woche in data["weeks"] for d in woche}
    assert tage["2026-09-08"]["status"] == "belegt"
    assert tage["2026-08-31"]["in_month"] is False


def test_kalender_ohne_angabe_nimmt_den_laufenden_monat(kalender_api):
    heute = date.today()

    data = kalender_api(NUTZER_1).get("/api/calendar").json()

    assert (data["year"], data["month"]) == (heute.year, heute.month)
    assert data["today"] == heute.isoformat()


def test_unsinnige_monate_werden_abgelehnt(kalender_api):
    client = kalender_api(NUTZER_1)

    assert client.get("/api/calendar", params={"month": 13}).status_code == 422
    assert client.get("/api/calendar", params={"year": 1900}).status_code == 422


def test_anderer_mandant_sieht_einen_leeren_kalender(kalender_api):
    data = kalender_api(NUTZER_2).get("/api/calendar", params={"year": 2026, "month": 9}).json()

    assert data["occupied_days"] == 0
    assert all(not d["bookings"] for woche in data["weeks"] for d in woche)


def test_buchungsdetail_zeigt_umbuchung_und_stornierung(kalender_api, session):
    client = kalender_api(NUTZER_1)
    kalender = client.get("/api/calendar", params={"year": 2026, "month": 9}).json()
    tage = {d["date"]: d for woche in kalender["weeks"] for d in woche}
    novak = next(b for b in tage["2026-09-10"]["bookings"] if b["guest_name"] == "Marta Novak")

    detail = client.get(f"/api/bookings/{novak['booking_id']}").json()

    assert detail["guest_name"] == "Marta Novak"
    assert detail["unit"] == "Haus Anna"
    assert detail["nights"] == 3
    assert [c["field"] for c in detail["changes"]] == ["arrival_date"]
    assert detail["changes"][0]["new_value"] == "2026-09-10"
    assert detail["cancellation"] is None

    storno = next(
        b for woche in kalender["weeks"] for d in woche for b in d["bookings"] if b["cancelled"]
    )
    storniert = client.get(f"/api/bookings/{storno['booking_id']}").json()
    assert storniert["status"] == "cancelled"
    assert storniert["cancellation"]["reason"] == "Flugausfall"


def test_fremde_buchung_liefert_404(kalender_api):
    erster = kalender_api(NUTZER_1)
    kalender = erster.get("/api/calendar", params={"year": 2026, "month": 9}).json()
    eine = next(b for woche in kalender["weeks"] for d in woche for b in d["bookings"])

    assert erster.get(f"/api/bookings/{eine['booking_id']}").status_code == 200
    assert kalender_api(NUTZER_2).get(f"/api/bookings/{eine['booking_id']}").status_code == 404


def test_kalender_braucht_eine_anmeldung(kalender_api):
    anonym = kalender_api()

    assert anonym.get("/api/calendar").status_code == 401
    assert anonym.get("/api/bookings/1").status_code == 401
