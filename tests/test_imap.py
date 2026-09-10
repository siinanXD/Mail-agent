"""IMAP-Nachrichten in ParsedEmail umwandeln."""

from __future__ import annotations

from email.message import EmailMessage

from app.email.imap_client import extract_body, parse_message


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
