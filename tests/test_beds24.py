"""Regelbasierte Extraktion der Beds24-Benachrichtigungen (synthetische Mails)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.email.beds24 import parse_beds24, parse_beds24_records
from app.email.parser import ParsedEmail


def _mail(subject: str, body: str, message_id: str = "beds24-test") -> ParsedEmail:
    return ParsedEmail(
        provider_message_id=message_id,
        subject=subject,
        body=body,
        received_at=datetime(2026, 6, 1, 12, 0),
    )


_GRUPPE_ZIMMER = (
    "Haus am See Zimmer Nr.1 Buchungsnummer: 87000030 Gruppen ID: 87000030 "
    "Personen 2 Preis €210.00 Zimmer Nr. 3 Buchungsnummer: 87000031 "
    "Gruppen ID: 87000030 Personen 2 Preis {preis} Check-in Fr 13 Aug 2027 "
    "Letzte Übernachtung Sa 14 Aug 2027 Check-out So 15 Aug 2027 "
    "Booking.com 6900000001 Name Max Muster Email max@example.com"
)

GRUPPE_BUCHUNG = _mail(
    "Buchung: Haus am See : Anzahl 2 - Fr 13 Aug 2027: 87000030 - Muster - Booking.com",
    "Neue Buchungsbenachrichtigung. " + _GRUPPE_ZIMMER.format(preis="€210.00"),
    message_id="mail-gruppe-buchung",
)

GRUPPE_STORNO = _mail(
    "Buchung storniert: Haus am See : Anzahl 2 - Fr 13 Aug 2027: 87000031 - Muster - Booking.com",
    "Diese Buchung wurde durch den Gast storniert. " + _GRUPPE_ZIMMER.format(preis="€0.00"),
    message_id="mail-gruppe-storno",
)


VARIANTE_NAME = _mail(
    "Buchung: Ferienwohnung Sonnenhang: Sonnenhang Booking - Sa 19 Sep 2026: "
    "88000001 - Beispiel - Booking.com",
    "Neue Buchungsbenachrichtigung. Ferienwohnung Sonnenhang Sonnenhang Booking "
    "Buchungsnummer: 88000001 Personen 4 Preis 199.00€ Check-in Sa 19 Sep 2026 "
    "Letzte Übernachtung Mo 21 Sep 2026 Check-out Di 22 Sep 2026 "
    "Booking.com 1234567890 Name Beispiel Erika Email erika@example.com "
    "Gesamtpreis 199.00€ Sprache DE ref hn88000001",
)

VARIANTE_GAST = _mail(
    "Buchung: Haus am See : Zimmer Nr.1 - Do 4 Feb 2027: 87000005 - Muster - Booking.com",
    "Neue Buchungsbenachrichtigung. Objekt: Haus am See Zimmer: Zimmer Nr. 1 "
    "Buchungsnummer: 87000005 Gast: Max Muster Personen: 2 Preis: 123.45 EUR "
    "Check-in: 04.02.2027 Check-out: 06.02.2027 Telefon: +49 170 0000000 "
    "Status: Neue Buchung",
)


def test_buchung_mit_name_variante():
    result = parse_beds24(VARIANTE_NAME)

    assert result is not None
    assert result.email_type == "booking"
    assert result.booking_reference == "88000001"
    assert result.guest_name == "Beispiel Erika"
    assert result.arrival_date == date(2026, 9, 19)
    assert result.departure_date == date(2026, 9, 22)
    # "Sonnenhang Booking" ist nur das Channel-Zimmer derselben Wohnung.
    assert result.unit_name == "Ferienwohnung Sonnenhang"


def test_buchung_mit_gast_variante_und_numerischen_daten():
    result = parse_beds24(VARIANTE_GAST)

    assert result is not None
    assert result.email_type == "booking"
    assert result.guest_name == "Max Muster"
    assert result.arrival_date == date(2027, 2, 4)
    assert result.departure_date == date(2027, 2, 6)
    # "Nr.1" und "Nr. 1" muessen dasselbe Objekt ergeben.
    assert result.unit_name == "Haus am See - Zimmer Nr. 1"


def test_weitergeleitete_gruppenstornierung_nutzt_nummer_aus_dem_betreff():
    result = parse_beds24(
        _mail(
            "Fwd: Buchung storniert: Haus am See : Anzahl 2 - Sa 1 Aug 2026: "
            "87000003 - Muster - Booking.com",
            "Gesendet mit der Mail App Von: bookings@beds24.com Betreff: Buchung "
            "storniert: Haus am See : Anzahl 2 - Sa 1 Aug 2026: 87000003 - Muster "
            "Diese Buchung wurde durch den Gast storniert. Haus am See Zimmer Nr.1 "
            "Buchungsnummer: 87000002 Gruppen ID: 87000002 Personen 2 Preis €0.00 "
            "Zimmer Nr. 3 Buchungsnummer: 87000003 Gruppen ID: 87000002 Personen 2 "
            "Check-in Sa 1 Aug 2026 Letzte Übernachtung Sa 1 Aug 2026 "
            "Check-out So 2 Aug 2026 Booking.com 6900000000 Name Max Muster "
            "Email max@example.com Gesamtpreis €0.00",
        )
    )

    assert result is not None
    assert result.email_type == "cancellation"
    assert result.booking_reference == "87000003"
    assert result.guest_name == "Max Muster"
    assert result.arrival_date == date(2026, 8, 1)
    assert result.departure_date == date(2026, 8, 2)
    # Nicht "Anzahl 2", sondern das Zimmer zur Nummer aus dem Betreff.
    assert result.unit_name == "Haus am See - Zimmer Nr. 3"


def test_gruppenstornierung_betrifft_nur_das_zimmer_aus_dem_betreff():
    """Beds24 schickt je storniertem Zimmer eine eigene Mail."""
    records = parse_beds24_records(GRUPPE_STORNO)

    assert [(r.booking_reference, r.unit_name) for r in records] == [
        ("87000031", "Haus am See - Zimmer Nr. 3")
    ]


def test_lange_mail_ohne_gruppen_id_blockiert_den_parser_nicht():
    """Das Muster lief quadratisch: 40 000 Zeichen brauchten ~8 s, 80 000 schon ~34 s."""
    import time

    from app.email.beds24 import _group_rooms

    body = "Buchungsnummer: 1 x " * 2000  # 40 000 Zeichen

    start = time.perf_counter()
    assert _group_rooms(body, "Haus am See") == {}
    assert time.perf_counter() - start < 1.0


def test_neue_gruppenbuchung_ergibt_je_zimmer_eine_buchung():
    """Fuer eine neue Gruppe gibt es nur eine Mail - beide Zimmer brauchen eine Buchung."""
    records = parse_beds24_records(GRUPPE_BUCHUNG)

    assert [(r.booking_reference, r.unit_name) for r in records] == [
        ("87000030", "Haus am See - Zimmer Nr. 1"),
        ("87000031", "Haus am See - Zimmer Nr. 3"),
    ]
    assert all(r.email_type == "booking" for r in records)
    assert all(r.guest_name == "Max Muster" for r in records)
    assert all(r.arrival_date == date(2027, 8, 13) for r in records)
    assert all(r.departure_date == date(2027, 8, 15) for r in records)


@pytest.mark.parametrize(
    ("room", "unit"),
    [
        ("Sonnenhang Air BNB", "Ferienwohnung Sonnenhang"),
        ("Sonnenhang Booking", "Ferienwohnung Sonnenhang"),
        ("Zimmer Nr.1", "Ferienwohnung Sonnenhang - Zimmer Nr. 1"),
        ("Einzelzimmer", "Ferienwohnung Sonnenhang - Einzelzimmer"),
    ],
)
def test_channel_zimmer_werden_zusammengelegt_echte_zimmer_nicht(room, unit):
    result = parse_beds24(
        _mail(
            f"Buchung: Ferienwohnung Sonnenhang: {room} - Sa 19 Sep 2026: 88000009 - X - Airbnb",
            "Neue Buchungsbenachrichtigung. Buchungsnummer: 88000009 "
            "Check-in Sa 19 Sep 2026 Check-out So 20 Sep 2026",
        )
    )

    assert result is not None
    assert result.unit_name == unit


def test_aenderung_liefert_nur_den_neuen_stand():
    result = parse_beds24(
        _mail(
            # Beds24 laesst hier das Leerzeichen nach dem Doppelpunkt weg.
            "Buchungsänderung: Ferienwohnung Sonnenhang:Sonnenhang Booking - "
            "Do 18 Jun 2026: 87000004 - Li - Airbnb",
            "Diese Buchung wurde durch den Gast geändert. Ferienwohnung Sonnenhang "
            "Buchungsnummer: 87000004 Personen 4 Preis 98.76€ Check-in Do 18 Jun 2026 "
            "Letzte Übernachtung Do 18 Jun 2026 Check-out Fr 19 Jun 2026 "
            "Airbnb HMABC12345 Name Wei Li Email 8600000000 zh",
        )
    )

    assert result is not None
    assert result.email_type == "change"
    assert result.new_arrival_date == date(2026, 6, 18)
    assert result.new_departure_date == date(2026, 6, 19)
    assert result.new_unit_name == "Ferienwohnung Sonnenhang"
    assert result.arrival_date is None
    assert result.unit_name is None


def test_unverbindliche_anfrage_ist_keine_buchung():
    result = parse_beds24(
        _mail(
            "Unverbindlich: Haus am See : Zimmer Nr. 4 - Do 30 Jul 2026: 90000006 -  - Airbnb",
            "Neue unverbindliche Anfrage. Buchungstyp: Unverbindlich. Haus am See "
            "Zimmer Nr. 4 Buchungsnummer: 90000006 Personen 2 Preis €54.32 "
            "Check-in Do 30 Jul 2026 Check-out Fr 31 Jul 2026 Airbnb 2600000000 "
            "Name Anna Email Gesamtpreis €54.32",
        )
    )

    assert result is not None
    assert result.email_type == "request"
    assert result.booking_reference == "90000006"
    assert result.guest_name == "Anna"


def test_deutscher_monat_maerz():
    result = parse_beds24(
        _mail(
            "Buchung: Haus am See : Einzelzimmer - So 1 Mär 2026: 87000007 - Muster - Airbnb",
            "Neue Buchungsbenachrichtigung. Buchungsnummer: 87000007 "
            "Check-in So 1 Mär 2026 Check-out Mo 2 Mär 2026 Name Max Muster Email",
        )
    )

    assert result is not None
    assert result.arrival_date == date(2026, 3, 1)
    assert result.departure_date == date(2026, 3, 2)


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        # Antwort eines Gastes auf die Benachrichtigung - keine neue Buchung.
        ("AW: " + VARIANTE_GAST.subject, VARIANTE_GAST.body),
        # Beds24-Testbuchung ohne das feste Betreffmuster.
        ("Buchung: Test Apartment", "Buchungsnummer: 87000008"),
        # Passender Betreff, aber der Text traegt die Nummer nicht.
        (VARIANTE_NAME.subject, "Hallo, ich habe eine Frage zur Anreise."),
        ("Buchung: Ferienwohnung 03.07.–08.07", "Max Muster 03.07.-08.07."),
        ("Newsletter: Die besten Angebote", "Jetzt zugreifen!"),
    ],
)
def test_fremde_mails_gehen_an_das_llm(subject, body):
    assert parse_beds24(_mail(subject, body)) is None
