""".eml-Dateien, manifest.csv und die Auswertung gegen Labels."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from app.email.beds24 import parse_beds24
from app.email.evaluate import evaluate_directory, main, render
from app.email.parser import load_directory, parse_file

#: Aufbau wie die Exporte: kodierter, gefalteter Betreff, quoted-printable,
#: kein Date- und kein Message-ID-Header.
AENDERUNG_EML = (
    b"Subject: =?utf-8?q?Buchungs=C3=A4nderung=3A_Haus_am_See_=3A?=\n"
    b" =?utf-8?q?_Zimmer_Nr=2E_3_-_Mo_3_Aug_2026=3A_87000020_-_Muster_-_Airbnb?=\n"
    b"From: bookings@beds24.com\n"
    b"To: vermietung@example.com\n"
    b"X-Intent: change\n"
    b"MIME-Version: 1.0\n"
    b'Content-Type: multipart/alternative; boundary="XYZ"\n'
    b"\n"
    b"--XYZ\n"
    b'Content-Type: text/plain; charset="utf-8"\n'
    b"Content-Transfer-Encoding: quoted-printable\n"
    b"\n"
    b"Diese Buchung wurde durch den Gast ge=C3=A4ndert. Buchungsnummer: 87000020 Pe=\n"
    b"rsonen 2 Check-in Mo 3 Aug 2026 Letzte =C3=9Cbernachtung Mo 3 Aug 2026 Check-o=\n"
    b"ut Mi 5 Aug 2026 Airbnb HMABC Name Max Muster Email ref hn87000020\n"
    b"\n"
    b"--XYZ\n"
    b'Content-Type: text/html; charset="utf-8"\n'
    b"\n"
    b"<html><body>Diese Buchung wurde durch den Gast ge&auml;ndert.</body></html>\n"
    b"--XYZ--\n"
)

BUCHUNG_EML = (
    "Subject: Buchung: Haus am See : Zimmer Nr. 3 - Mo 3 Aug 2026: 87000021 - Beispiel - Airbnb\n"
    "From: bookings@beds24.com\n"
    "Message-ID: <buchung-87000021@example.com>\n"
    "Date: Tue, 02 Jun 2026 08:15:00 +0000\n"
    'Content-Type: text/plain; charset="utf-8"\n'
    "Content-Transfer-Encoding: 8bit\n"
    "\n"
    "Neue Buchungsbenachrichtigung. Buchungsnummer: 87000021 Check-in Mo 3 Aug 2026 "
    "Check-out Di 4 Aug 2026 Name Erika Beispiel Email\n"
).encode("utf-8")

SPAM_EML = (
    b"Subject: Nur heute: 25% auf alles\n"
    b"From: news@shop.example\n"
    b"Content-Type: text/plain; charset=utf-8\n"
    b"\n"
    b"Jetzt zugreifen!\n"
)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _manifest(directory: Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ["intent,file,account_id,platform,processing_state,received_at,subject"]
    lines += [f"{intent},{file},acc,outlook,approved,{received},x" for intent, file, received in rows]
    (directory / "manifest.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_eml_ohne_header_nutzt_dateiname_und_manifest_datum(tmp_path):
    path = _write(tmp_path / "change" / "001_aenderung.eml", AENDERUNG_EML)

    email = parse_file(path, received_at=datetime(2026, 6, 19, 18, 7))

    assert email.provider_message_id.startswith("001_aenderung-")
    assert email.received_at == datetime(2026, 6, 19, 18, 7)
    assert email.subject == (
        "Buchungsänderung: Haus am See : Zimmer Nr. 3 - Mo 3 Aug 2026: "
        "87000020 - Muster - Airbnb"
    )
    # text/plain wird bevorzugt und quoted-printable korrekt zusammengefuegt.
    assert "geändert" in email.body
    assert "Check-out Mi 5 Aug 2026" in " ".join(email.body.split())

    result = parse_beds24(email)
    assert result is not None
    assert result.email_type == "change"
    assert result.new_departure_date == date(2026, 8, 5)


def test_date_header_hat_vorrang_vor_dem_manifest(tmp_path):
    path = _write(tmp_path / "001_buchung.eml", BUCHUNG_EML)

    email = parse_file(path, received_at=datetime(2030, 1, 1))

    assert email.provider_message_id == "<buchung-87000021@example.com>"
    assert email.received_at.date() == date(2026, 6, 2)


def test_load_directory_liest_unterordner_mit_manifest(tmp_path):
    _write(tmp_path / "change" / "001_aenderung.eml", AENDERUNG_EML)
    _write(tmp_path / "other" / "002_spam.eml", SPAM_EML)
    _manifest(
        tmp_path,
        [
            ("change", "change/001_aenderung.eml", "2026-06-19T18:07:34Z"),
            ("other", "other/002_spam.eml", "2026-05-01T09:00:00Z"),
        ],
    )

    emails = load_directory(tmp_path)

    assert [mail.provider_message_id.rsplit("-", 1)[0] for mail in emails] == [
        "002_spam",
        "001_aenderung",
    ]
    assert emails[1].received_at == datetime(2026, 6, 19, 18, 7, 34)


def test_gleichnamige_dateien_in_unterordnern_bekommen_verschiedene_ids(tmp_path):
    """Frueher war die Ersatz-ID nur der Dateiname - die zweite Mail ueberschrieb die erste."""
    eingang = parse_file(_write(tmp_path / "inbox" / "001.eml", AENDERUNG_EML))
    archiv = parse_file(_write(tmp_path / "archiv" / "001.eml", SPAM_EML))
    kopie = parse_file(_write(tmp_path / "kopie" / "001.eml", AENDERUNG_EML))

    assert eingang.provider_message_id != archiv.provider_message_id
    # Gleicher Inhalt ist wirklich dieselbe Mail - die Dublettenpruefung soll greifen.
    assert eingang.provider_message_id == kopie.provider_message_id
    assert eingang.provider_message_id.startswith("001-")

    ergebnis = load_directory(tmp_path)
    assert len({mail.provider_message_id for mail in ergebnis}) == 2


def _labeled_dataset(directory: Path) -> None:
    _write(directory / "change" / "001_aenderung.eml", AENDERUNG_EML)
    # Falsch gelabelt: eine Buchungsmail im Ordner "cancellation".
    _write(directory / "cancellation" / "002_buchung.eml", BUCHUNG_EML)
    _write(directory / "other" / "003_spam.eml", SPAM_EML)
    _write(directory / "guest_inquiry" / "004_spam.eml", SPAM_EML)
    _write(directory / "other" / "005_ohne_label.eml", SPAM_EML)
    _manifest(
        directory,
        [
            ("change", "change/001_aenderung.eml", "2026-06-19T18:07:34Z"),
            ("cancellation", "cancellation/002_buchung.eml", "2026-06-02T08:15:00Z"),
            ("other", "other/003_spam.eml", "2026-06-03T10:00:00Z"),
            ("guest_inquiry", "guest_inquiry/004_spam.eml", "2026-06-04T10:00:00Z"),
        ],
    )


def test_auswertung_gegen_manifest_labels(tmp_path):
    _labeled_dataset(tmp_path)

    report = evaluate_directory(tmp_path)

    assert len(report.samples) == 3
    assert report.skipped == 1
    assert report.unlabeled == ["other/005_ohne_label.eml"]
    assert report.failed == []

    stats = report.per_label()
    assert (stats["change"].recognized, stats["change"].correct) == (1, 1)
    assert (stats["cancellation"].recognized, stats["cancellation"].correct) == (1, 0)
    assert (stats["other"].recognized, stats["other"].correct) == (0, 0)
    assert report.confusion()[("cancellation", "booking")] == 1
    # Die Aenderung 87000020 hat keine Buchungsmail im Datensatz.
    assert [s.booking_reference for s in report.orphans()] == ["87000020"]

    text = render(report)
    assert "Abweichungen (1)" in text
    assert "cancellation/002_buchung.eml: cancellation -> booking" in text


def test_auswertung_mit_allen_labels(tmp_path):
    _labeled_dataset(tmp_path)

    report = evaluate_directory(tmp_path, include_unreliable=True)

    assert report.skipped == 0
    assert "guest_inquiry" in report.per_label()


def test_cli_ohne_manifest_meldet_fehler(tmp_path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "manifest.csv" in capsys.readouterr().err


def test_cli_gibt_bericht_aus(tmp_path, capsys):
    _labeled_dataset(tmp_path)

    assert main([str(tmp_path)]) == 0
    assert "Gewertet: 3 Mails" in capsys.readouterr().out
