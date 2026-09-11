"""Parsing, Extraktion und der komplette Ingestion-Pfad."""

from __future__ import annotations

from datetime import date, datetime

from app.database import repositories as repo
from app.email.extractor import EmailExtraction, extract
from app.email.parser import (
    ParsedEmail,
    load_directory,
    parse_file,
    parse_json,
    parse_text,
)
from app.email.importer import import_directory, import_email
from app.knowledge.chunker import chunk_email, chunk_text
from tests.fakes import (
    StructuredOutputStub,
    fake_embedder,
    rule_based_extractor,
)

RAW = """Message-ID: mail-4711
From: gast@example.com
To: reservierung@hotel-seeblick.de
Subject: Buchung BK-2026-0999
Date: 2026-08-01T10:30:00

Guten Tag,

Anreise: 03.09.2026
Abreise: 07.09.2026

Viele Gruesse
"""


def test_parse_text_liest_header_und_body():
    email = parse_text(RAW, fallback_id="unused")

    assert email.provider_message_id == "mail-4711"
    assert email.sender == "gast@example.com"
    assert email.subject == "Buchung BK-2026-0999"
    assert email.received_at == datetime(2026, 8, 1, 10, 30)
    assert email.body.startswith("Guten Tag,")
    assert "Anreise: 03.09.2026" in email.body


def test_parse_json_mit_deutschem_datum():
    content = (
        '{"provider_message_id": "mail-1", "sender": "a@b.de", '
        '"subject": "Test", "body": "Hallo", "received_at": "01.08.2026 09:00"}'
    )
    email = parse_json(content, fallback_id="x")

    assert email.received_at == datetime(2026, 8, 1, 9, 0)
    assert email.subject == "Test"


def test_load_directory_ist_chronologisch(sample_dir):
    emails = load_directory(sample_dir)

    assert len(emails) >= 8
    assert [e.received_at for e in emails] == sorted(e.received_at for e in emails)
    assert {e.provider_message_id for e in emails} >= {"mail-0001", "mail-0007"}


def test_extraktion_der_demo_mail():
    email = parse_text(RAW, fallback_id="unused")
    result = rule_based_extractor(email)

    assert result.email_type == "booking"
    assert result.booking_reference == "BK-2026-0999"
    assert result.arrival_date == date(2026, 9, 3)
    assert result.departure_date == date(2026, 9, 7)


def test_extract_nutzt_structured_output():
    """extract() reicht Betreff und Body an das Modell und gibt das Schema zurueck."""
    expected = EmailExtraction(email_type="complaint", guest_name="Sabine Krause")
    model = StructuredOutputStub(result=expected)
    email = ParsedEmail(
        provider_message_id="mail-x",
        subject="Beschwerde zum Fruehstueck",
        body="Der Kaffee war kalt.",
        received_at=datetime(2026, 9, 3, 19, 55),
    )

    result = extract(email, model=model)

    assert result is expected
    assert "Beschwerde zum Fruehstueck" in model.prompts[0]
    assert "Der Kaffee war kalt." in model.prompts[0]


def test_chunker_haengt_betreff_an_jeden_chunk():
    chunks = chunk_email("Stornierung", "Absatz eins.\n\nAbsatz zwei.")

    assert chunks
    assert all(chunk.startswith("Betreff: Stornierung") for chunk in chunks)
    assert chunk_text("") == []


def test_import_directory_legt_buchungen_und_stornierungen_an(session, sample_dir):
    """Voller Ingestion-Pfad auf den Demo-Daten (ohne pgvector-Indexierung)."""
    result = import_directory(
        session,
        sample_dir,
        extractor=rule_based_extractor,
        embedder=lambda chunks: [],
    )

    assert result.failed == []
    assert result.imported == 14
    assert result.bookings == 8
    assert result.cancellations == 3
    assert result.changes == 1
    # "Ferienwohnung Seeblick", "FeWo Seeblick" und "Ferienwohnung Bergblick"
    # duerfen nur drei Objekte ergeben.
    assert result.units == 3

    berger = repo.get_booking_by_reference(session, "BK-2026-0103")
    assert berger is not None
    assert berger.guest_name == "Thomas Berger"
    assert berger.arrival_date == date(2026, 9, 12)
    assert berger.status == "cancelled"

    cancellations = repo.search_cancellations(session, guest_name="Thomas Berger")
    assert len(cancellations) == 1
    assert cancellations[0].reason == "Flugausfall"
    assert cancellations[0].source_email_id is not None


def test_import_ist_idempotent(session, sample_dir):
    for _ in range(2):
        import_directory(
            session,
            sample_dir,
            extractor=rule_based_extractor,
            embedder=lambda chunks: [],
        )

    assert len(repo.search_emails(session, limit=100)) == 14
    assert len(repo.search_bookings(session, limit=100)) == 8
    assert len(repo.list_units(session)) == 3
    assert repo.count_cancellations(session) == 3


def test_kaputte_datei_stoppt_den_import_nicht(session, sample_dir, tmp_path):
    """Eine unlesbare Datei landet in failed, alle anderen werden importiert."""
    for file in sample_dir.iterdir():
        (tmp_path / file.name).write_bytes(file.read_bytes())
    (tmp_path / "99_kaputt.json").write_text("{ kein valides json", encoding="utf-8")

    result = import_directory(
        session, tmp_path, extractor=rule_based_extractor, embedder=lambda c: []
    )

    assert result.imported == 14
    assert len(result.failed) == 1
    assert "99_kaputt.json" in result.failed[0]


def test_fehlerhafte_mail_verwirft_nur_sich_selbst(session, sample_dir):
    """Savepoint pro Mail: vorherige Importe bleiben erhalten."""

    def extractor_mit_fehler(email):
        if email.provider_message_id == "mail-0004":
            raise ValueError("Extraktion kaputt")
        return rule_based_extractor(email)

    result = import_directory(
        session, sample_dir, extractor=extractor_mit_fehler, embedder=lambda c: []
    )
    session.commit()

    assert result.imported == 13
    assert result.failed == ["mail-0004: Extraktion kaputt"]
    assert len(repo.search_emails(session, limit=100)) == 13
    assert repo.get_booking_by_reference(session, "BK-2026-0101") is not None
    assert repo.get_booking_by_reference(session, "BK-2026-0104") is None


def test_stornierte_buchung_bleibt_storniert(session, sample_dir):
    """Erneuter Import nur der aelteren Buchungsmail setzt nicht auf confirmed."""
    import_directory(
        session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: []
    )
    nur_buchung = [
        p for p in sample_dir.iterdir() if p.name == "03_buchung_berger.json"
    ]
    assert nur_buchung

    import_email(
        session,
        parse_file(nur_buchung[0]),
        extractor=rule_based_extractor,
        embedder=lambda c: [],
    )
    session.commit()

    booking = repo.get_booking_by_reference(session, "BK-2026-0103")
    assert booking.status == "cancelled"


def test_buchung_ohne_gastnamen_ist_importierbar(session):
    """guest_name ist optional - der Import darf daran nicht scheitern."""
    parsed = ParsedEmail(
        provider_message_id="mail-ohne-namen",
        subject="Buchung BK-2026-0777",
        body="Buchungsnummer: BK-2026-0777\nAnreise: 01.11.2026",
        received_at=datetime(2026, 10, 1, 8, 0),
    )

    import_email(
        session,
        parsed,
        extractor=lambda mail: EmailExtraction(
            email_type="booking",
            booking_reference="BK-2026-0777",
            guest_name=None,
            arrival_date=date(2026, 11, 1),
        ),
        embedder=lambda c: [],
    )
    session.commit()

    booking = repo.get_booking_by_reference(session, "BK-2026-0777")
    assert booking is not None
    assert booking.guest_name == ""


def test_stornierung_ohne_nummer_bleibt_bei_mehrdeutigkeit_unverknuepft(session):
    """Zwei Buchungen desselben Gastes: keine wird versehentlich storniert."""
    for reference, arrival in (
        ("BK-2026-0801", date(2026, 11, 1)),
        ("BK-2026-0802", date(2026, 12, 1)),
    ):
        repo.upsert_booking(
            session,
            booking_reference=reference,
            guest_name="Lena Schulz",
            arrival_date=arrival,
            departure_date=None,
        )
    session.commit()

    import_email(
        session,
        ParsedEmail(
            provider_message_id="mail-storno-unklar",
            subject="Stornierung",
            body="Ich muss leider stornieren.",
            received_at=datetime(2026, 10, 15, 9, 0),
        ),
        extractor=lambda mail: EmailExtraction(
            email_type="cancellation",
            guest_name="Lena Schulz",
            cancellation_reason="Krankheit",
        ),
        embedder=lambda c: [],
    )
    session.commit()

    assert all(b.status == "confirmed" for b in repo.search_bookings(session))
    cancellations = repo.search_cancellations(session)
    assert len(cancellations) == 1
    assert cancellations[0].booking_id is None


def test_stornierung_ohne_nummer_trifft_eindeutige_buchung(session):
    repo.upsert_booking(
        session,
        booking_reference="BK-2026-0803",
        guest_name="Lena Schulz",
        arrival_date=date(2026, 11, 1),
        departure_date=None,
    )
    session.commit()

    import_email(
        session,
        ParsedEmail(
            provider_message_id="mail-storno-klar",
            subject="Stornierung",
            body="Bitte stornieren.",
            received_at=datetime(2026, 10, 15, 9, 0),
        ),
        extractor=lambda mail: EmailExtraction(
            email_type="cancellation", guest_name="Lena Schulz"
        ),
        embedder=lambda c: [],
    )
    session.commit()

    booking = repo.get_booking_by_reference(session, "BK-2026-0803")
    assert booking.status == "cancelled"
    assert repo.search_cancellations(session)[0].booking_id == booking.id


def test_stornierung_ohne_nummer_behaelt_zuordnung_beim_reimport(session):
    """Erneuter Import derselben Stornomail darf die Verknuepfung nicht kappen."""
    repo.upsert_booking(
        session,
        booking_reference="BK-2026-0804",
        guest_name="Lena Schulz",
        arrival_date=date(2026, 11, 1),
        departure_date=None,
    )
    session.commit()

    parsed = ParsedEmail(
        provider_message_id="mail-storno-reimport",
        subject="Stornierung",
        body="Bitte stornieren.",
        received_at=datetime(2026, 10, 15, 9, 0),
    )
    extractor = lambda mail: EmailExtraction(  # noqa: E731
        email_type="cancellation", guest_name="Lena Schulz"
    )

    for _ in range(2):
        import_email(session, parsed, extractor=extractor, embedder=lambda c: [])
    session.commit()

    booking = repo.get_booking_by_reference(session, "BK-2026-0804")
    cancellations = repo.search_cancellations(session)
    assert len(cancellations) == 1
    assert cancellations[0].booking_id == booking.id


def test_anreisedatum_muss_zur_buchung_passen(session):
    """Eine einzelne Buchung wird nicht storniert, wenn das Datum abweicht."""
    repo.upsert_booking(
        session,
        booking_reference="BK-2026-0805",
        guest_name="Lena Schulz",
        arrival_date=date(2026, 12, 1),
        departure_date=None,
    )
    session.commit()

    import_email(
        session,
        ParsedEmail(
            provider_message_id="mail-storno-falsches-datum",
            subject="Stornierung",
            body="Ich storniere meinen Aufenthalt im November.",
            received_at=datetime(2026, 10, 15, 9, 0),
        ),
        extractor=lambda mail: EmailExtraction(
            email_type="cancellation",
            guest_name="Lena Schulz",
            arrival_date=date(2026, 11, 1),
        ),
        embedder=lambda c: [],
    )
    session.commit()

    assert repo.get_booking_by_reference(session, "BK-2026-0805").status == "confirmed"
    assert repo.search_cancellations(session)[0].booking_id is None


def test_buchungsmail_ersetzt_platzhalter_quelle(session):
    """Storno vor Buchung: die spaetere Buchungsmail wird zur Quelle."""
    storno = ParsedEmail(
        provider_message_id="mail-storno-zuerst",
        subject="Stornierung BK-2026-0806",
        body="Ich storniere BK-2026-0806.",
        received_at=datetime(2026, 10, 15, 9, 0),
    )
    import_email(
        session,
        storno,
        extractor=lambda mail: EmailExtraction(
            email_type="cancellation",
            booking_reference="BK-2026-0806",
            guest_name="Jan Peters",
        ),
        embedder=lambda c: [],
    )
    buchung = ParsedEmail(
        provider_message_id="mail-buchung-spaeter",
        subject="Buchung BK-2026-0806",
        body="Anreise 01.12.2026",
        received_at=datetime(2026, 9, 1, 9, 0),
    )
    import_email(
        session,
        buchung,
        extractor=lambda mail: EmailExtraction(
            email_type="booking",
            booking_reference="BK-2026-0806",
            guest_name="Jan Peters",
            arrival_date=date(2026, 12, 1),
        ),
        embedder=lambda c: [],
    )
    session.commit()

    booking = repo.get_booking_by_reference(session, "BK-2026-0806")
    quelle = repo.get_email(session, booking.source_email_id)
    assert quelle.provider_message_id == "mail-buchung-spaeter"
    assert booking.status == "cancelled"


def test_aehnlicher_gastname_storniert_nicht_die_fremde_buchung(session):
    """"Anna Schmidt" darf nicht die Buchung von "Hanna Schmidt" stornieren."""
    repo.upsert_booking(
        session,
        booking_reference="BK-2026-0807",
        guest_name="Hanna Schmidt",
        arrival_date=date(2026, 11, 1),
        departure_date=None,
    )
    session.commit()

    import_email(
        session,
        ParsedEmail(
            provider_message_id="mail-storno-namensdreher",
            subject="Stornierung",
            body="Bitte stornieren.",
            received_at=datetime(2026, 10, 15, 9, 0),
        ),
        extractor=lambda mail: EmailExtraction(
            email_type="cancellation", guest_name="Anna Schmidt"
        ),
        embedder=lambda c: [],
    )
    session.commit()

    assert repo.get_booking_by_reference(session, "BK-2026-0807").status == "confirmed"
    assert repo.search_cancellations(session)[0].booking_id is None


def test_aenderung_ohne_buchungsmail_legt_buchung_an(session):
    """Beds24 schickt den kompletten neuen Stand - die Aenderung geht nicht verloren."""
    import_email(
        session,
        ParsedEmail(
            provider_message_id="mail-aenderung-ohne-buchung",
            subject="Buchungsänderung 87000010",
            body="Diese Buchung wurde durch den Gast geändert.",
            received_at=datetime(2026, 6, 19, 18, 0),
        ),
        extractor=lambda mail: EmailExtraction(
            email_type="change",
            booking_reference="87000010",
            guest_name="Wei Li",
            new_arrival_date=date(2026, 6, 18),
            new_departure_date=date(2026, 6, 19),
            new_unit_name="Haus am See - Zimmer Nr. 3",
        ),
        embedder=lambda c: [],
    )
    session.commit()

    booking = repo.get_booking_by_reference(session, "87000010")
    assert booking is not None
    assert booking.status == "confirmed"
    assert booking.arrival_date == date(2026, 6, 18)
    assert booking.departure_date == date(2026, 6, 19)
    assert booking.unit.name == "Haus am See - Zimmer Nr. 3"
    assert repo.search_booking_changes(session, booking_reference="87000010") == []


def test_aenderung_mit_unbekannter_nummer_trifft_keine_fremde_buchung(session):
    """Gleicher Gastname, andere Buchungsnummer: die Buchung bleibt unveraendert."""
    repo.upsert_booking(
        session,
        booking_reference="BK-2026-0808",
        guest_name="Wei Li",
        arrival_date=date(2026, 11, 1),
        departure_date=date(2026, 11, 3),
    )
    session.commit()

    import_email(
        session,
        ParsedEmail(
            provider_message_id="mail-aenderung-fremde-nummer",
            subject="Änderung 87000011",
            body="Wir bleiben zwei Nächte länger.",
            received_at=datetime(2026, 10, 1, 9, 0),
        ),
        extractor=lambda mail: EmailExtraction(
            email_type="change",
            booking_reference="87000011",
            guest_name="Wei Li",
            new_departure_date=date(2026, 11, 5),
        ),
        embedder=lambda c: [],
    )
    session.commit()

    booking = repo.get_booking_by_reference(session, "BK-2026-0808")
    assert booking.departure_date == date(2026, 11, 3)


def test_beds24_gruppe_je_zimmer_buchen_und_einzeln_stornieren(session):
    """Standard-Extractor ohne LLM: Gruppenbuchung und Storno eines Zimmers."""
    from tests.test_beds24 import GRUPPE_BUCHUNG, GRUPPE_STORNO

    buchung = import_email(session, GRUPPE_BUCHUNG, embedder=lambda c: [])
    storno = import_email(session, GRUPPE_STORNO, embedder=lambda c: [])
    session.commit()

    assert buchung.bookings == 2
    assert storno.cancellations == 1
    zimmer_1 = repo.get_booking_by_reference(session, "87000030")
    zimmer_3 = repo.get_booking_by_reference(session, "87000031")
    assert (zimmer_1.status, zimmer_1.unit.name) == ("confirmed", "Haus am See - Zimmer Nr. 1")
    assert (zimmer_3.status, zimmer_3.unit.name) == ("cancelled", "Haus am See - Zimmer Nr. 3")
    assert repo.count_cancellations(session) == 1


def test_storno_ohne_buchungsmail_behaelt_das_zimmer(session):
    """Legt erst die Stornomail die Buchung an, gehoert sie trotzdem zum Zimmer."""
    from tests.test_beds24 import GRUPPE_STORNO

    import_email(session, GRUPPE_STORNO, embedder=lambda c: [])
    session.commit()

    booking = repo.get_booking_by_reference(session, "87000031")
    assert booking.status == "cancelled"
    assert booking.unit.name == "Haus am See - Zimmer Nr. 3"


def _importiere(session, message_id: str, received_at: datetime, extraction: EmailExtraction):
    import_email(
        session,
        ParsedEmail(
            provider_message_id=message_id,
            subject=f"Mail {message_id}",
            body="Text",
            received_at=received_at,
        ),
        extractor=lambda mail: extraction,
        embedder=lambda c: [],
    )
    session.flush()


BUCHUNG_0901 = EmailExtraction(
    email_type="booking",
    booking_reference="BK-2026-0901",
    guest_name="Ida Nord",
    arrival_date=date(2026, 12, 1),
    departure_date=date(2026, 12, 5),
)


def test_erneuter_import_der_buchungsmail_setzt_umbuchung_nicht_zurueck(session):
    """Frueher ueberschrieb die aeltere Buchungsmail den umgebuchten Zeitraum."""
    _importiere(session, "buchung", datetime(2026, 9, 1, 9, 0), BUCHUNG_0901)
    _importiere(
        session,
        "umbuchung",
        datetime(2026, 9, 10, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_arrival_date=date(2026, 12, 3),
        ),
    )

    _importiere(session, "buchung", datetime(2026, 9, 1, 9, 0), BUCHUNG_0901)

    booking = repo.get_booking_by_reference(session, "BK-2026-0901")
    assert (booking.arrival_date, booking.departure_date) == (date(2026, 12, 3), date(2026, 12, 5))


def test_buchung_ohne_gespeicherten_stand_leitet_ihn_aus_den_aenderungen_ab(session):
    """Buchungen von vor Migration 0005 haben state_as_of = NULL."""
    _importiere(session, "buchung", datetime(2026, 9, 1, 9, 0), BUCHUNG_0901)
    _importiere(
        session,
        "umbuchung",
        datetime(2026, 9, 10, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_arrival_date=date(2026, 12, 3),
        ),
    )
    booking = repo.get_booking_by_reference(session, "BK-2026-0901")
    booking.state_as_of = None
    session.flush()

    _importiere(session, "buchung", datetime(2026, 9, 1, 9, 0), BUCHUNG_0901)

    assert repo.get_booking_by_reference(session, "BK-2026-0901").arrival_date == date(2026, 12, 3)


def test_aeltere_buchungsmail_nach_umbuchung_fuellt_nur_luecken(session):
    """Umbuchung zuerst importiert: die spaeter eintreffende Buchungsmail ist aelter."""
    _importiere(
        session,
        "umbuchung-zuerst",
        datetime(2026, 9, 10, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_arrival_date=date(2026, 12, 3),
            new_departure_date=date(2026, 12, 6),
        ),
    )

    _importiere(session, "buchung", datetime(2026, 9, 1, 9, 0), BUCHUNG_0901)

    booking = repo.get_booking_by_reference(session, "BK-2026-0901")
    assert (booking.arrival_date, booking.departure_date) == (date(2026, 12, 3), date(2026, 12, 6))
    assert booking.guest_name == "Ida Nord"  # die Luecke fuellt sie trotzdem


def test_aeltere_umbuchung_ueberschreibt_die_neuere_nicht(session):
    _importiere(session, "buchung", datetime(2026, 9, 1, 9, 0), BUCHUNG_0901)
    _importiere(
        session,
        "umbuchung-neu",
        datetime(2026, 9, 20, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_departure_date=date(2026, 12, 8),
        ),
    )

    _importiere(
        session,
        "umbuchung-alt",
        datetime(2026, 9, 10, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_departure_date=date(2026, 12, 6),
        ),
    )

    booking = repo.get_booking_by_reference(session, "BK-2026-0901")
    assert booking.departure_date == date(2026, 12, 8)
    # Protokolliert wird sie trotzdem - ohne falsches "vorher".
    alt = [c for c in repo.search_booking_changes(session) if c.new_value == "2026-12-06"]
    assert len(alt) == 1
    assert alt[0].old_value is None


def test_aeltere_umbuchung_eines_anderen_felds_wird_noch_uebernommen(session):
    """Zimmerwechsel scheitert erst, eine spaetere Abreise-Umbuchung klappt.

    Der wiederholte Zimmerwechsel ist aelter, betrifft aber ein anderes Feld -
    frueher wurde er verworfen und die Buchung blieb im alten Zimmer.
    """
    _importiere(
        session,
        "buchung-mit-zimmer",
        datetime(2026, 9, 1, 9, 0),
        EmailExtraction(
            email_type="booking",
            booking_reference="BK-2026-0901",
            guest_name="Ida Nord",
            arrival_date=date(2026, 12, 1),
            departure_date=date(2026, 12, 5),
            unit_name="Wohnung Nord",
        ),
    )
    _importiere(
        session,
        "abreise-neu",
        datetime(2026, 9, 20, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_departure_date=date(2026, 12, 8),
        ),
    )

    _importiere(
        session,
        "zimmerwechsel-alt",
        datetime(2026, 9, 10, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_unit_name="Wohnung Sued",
        ),
    )

    booking = repo.get_booking_by_reference(session, "BK-2026-0901")
    assert "Sued" in booking.unit.name
    assert booking.departure_date == date(2026, 12, 8)


def test_umbuchung_auf_den_aktuellen_wert_zaehlt_als_neuester_stand(session):
    """Eine neuere Umbuchung bestaetigt die Abreise, eine aeltere kommt spaeter an.

    Frueher wurde fuer den unveraenderten Wert nichts vermerkt - die aeltere
    Umbuchung ueberschrieb die Abreise danach mit ihrem veralteten Datum.
    """
    _importiere(session, "buchung", datetime(2026, 9, 1, 9, 0), BUCHUNG_0901)
    _importiere(
        session,
        "abreise-bestaetigt",
        datetime(2026, 9, 20, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_departure_date=date(2026, 12, 5),
        ),
    )

    _importiere(
        session,
        "abreise-zwischenstand",
        datetime(2026, 9, 10, 9, 0),
        EmailExtraction(
            email_type="change",
            booking_reference="BK-2026-0901",
            new_departure_date=date(2026, 12, 8),
        ),
    )

    booking = repo.get_booking_by_reference(session, "BK-2026-0901")
    assert booking.departure_date == date(2026, 12, 5)
    # Die Bestaetigung ist nur ein Zeitstempel, keine sichtbare Umbuchung.
    sichtbar = repo.search_booking_changes(session, booking_reference="BK-2026-0901")
    assert [(c.old_value, c.new_value) for c in sichtbar] == [(None, "2026-12-08")]


def test_buchungsnummer_mit_unterstrich_trifft_keine_fremde_buchung(session):
    """"_" war ein LIKE-Platzhalter: eine Storno fuer BK_123 stornierte BK-123."""
    repo.upsert_booking(
        session,
        booking_reference="BK-123",
        guest_name="Ida Nord",
        arrival_date=date(2026, 12, 1),
        departure_date=date(2026, 12, 5),
    )
    session.commit()

    assert repo.get_booking_by_reference(session, "BK_123") is None
    assert repo.get_booking_by_reference(session, "bk-123") is not None  # Schreibweise egal

    _importiere(
        session,
        "storno-unterstrich",
        datetime(2026, 9, 5, 9, 0),
        EmailExtraction(email_type="cancellation", booking_reference="BK_123", guest_name="Ida Nord"),
    )

    assert repo.get_booking_by_reference(session, "BK-123").status == "confirmed"
    assert repo.search_emails(session, booking_reference="BK-123") == []


def test_fake_embedder_liefert_konfigurierte_dimension():
    vectors = fake_embedder(["hallo welt", "hallo welt"])

    assert len(vectors) == 2
    assert vectors[0] == vectors[1]
    assert len(vectors[0]) == 1536
