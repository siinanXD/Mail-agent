"""Regelbasierte Extraktion fuer Benachrichtigungen des Channel-Managers Beds24.

Beds24 verschickt Buchungen, Stornierungen und Aenderungen in festem Format:

    Betreff: Buchung storniert: Ferienhaus Beispiel : Zimmer Nr. 3 - Sa 1 Aug 2026: 87000001 - Muster - Booking.com
    Text:    Diese Buchung wurde durch den Gast storniert. ... Buchungsnummer: 87000001
             Personen 2 Preis 0.00€ Check-in Sa 1 Aug 2026 ... Check-out So 2 Aug 2026
             Booking.com 1234567890 Name Max Muster Email ...

Fuer diese Mails braucht es kein LLM: Buchungsnummer und Daten kommen exakt aus
dem Text, und es fallen keine Kosten an. Passt eine Mail nicht in das Muster,
liefert ``parse_beds24`` ``None`` und die Mail geht an die LLM-Extraktion.

Gruppenbuchungen ("Anzahl 2") listen im Text jedes Zimmer mit eigener Nummer:

    Ferienhaus Beispiel Zimmer Nr.1 Buchungsnummer: 87000010 Gruppen ID: 87000010 Personen 2
    Preis €210.00 Zimmer Nr. 3 Buchungsnummer: 87000011 Gruppen ID: 87000010 ...
"""

from __future__ import annotations

import re
from datetime import date

from app.email.extractor import EmailExtraction, EmailType
from app.email.parser import ParsedEmail
from app.units import normalize_unit_name

_KIND_TO_TYPE: dict[str, EmailType] = {
    "buchung": "booking",
    "buchung storniert": "cancellation",
    "buchungsänderung": "change",
    # Unverbindliche Anfrage: noch keine Buchung, der Gast hat nur angefragt.
    "unverbindlich": "request",
}

_MONTHS = {
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "mar": 3, "apr": 4, "mai": 5,
    "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
    "nov": 11, "dez": 12, "dec": 12,
}  # fmt: skip

#: Nur Weiterleitungen entfernen. "AW:"/"Re:" ist die Antwort eines Gastes und
#: darf nicht als neue Buchung durchgehen.
_FORWARD_PREFIX = re.compile(r"^(?:\s*(?:fwd?|wg)\s*:)+\s*", re.IGNORECASE)

_WORD = r"[^\W\d_]"
_SUBJECT = re.compile(
    rf"^(?P<kind>Buchung storniert|Buchungsänderung|Buchung|Unverbindlich)\s*:\s*"
    rf"(?P<unit>.+?)\s+-\s+{_WORD}{{2,3}}\.?\s+"
    rf"(?P<day>\d{{1,2}})\.?\s+(?P<month>{_WORD}+)\.?\s+(?P<year>\d{{4}})\s*:\s*"
    r"(?P<reference>\d{6,})\s+-\s*(?P<guest>.*?)\s*-\s*(?P<channel>[^-]+?)$",
    re.IGNORECASE,
)

_DATE = rf"\d{{1,2}}\.\d{{1,2}}\.\d{{4}}|\d{{1,2}}\.?\s+{_WORD}+\.?\s+\d{{4}}"
_WEEKDAY = rf"(?:{_WORD}{{2,3}}\.?,?\s+)?"

#: Zwei Textvarianten: "Name Muster Max Email ..." und "Gast: Max Muster Personen: 2".
_GUEST_PATTERNS = (
    re.compile(r"\bName\s+(?P<name>.+?)\s+E-?[Mm]ail\b"),
    re.compile(r"\bGast:\s*(?P<name>.+?)\s+(?:Personen|Check-in|Telefon|E-?[Mm]ail)\b"),
)
_MAX_NAME_LENGTH = 80

#: Beds24 legt eine Wohnung je Vertriebskanal als eigenes "Zimmer" an.
_CHANNEL_SUFFIX = re.compile(r"\s*(?:air\s*bnb|booking(?:\.com)?)\s*$", re.IGNORECASE)

_GROUP_ROOM = re.compile(r"Buchungsnummer:?\s*(?P<reference>\d+)\s+Gruppen\s*ID:?\s*\d+")
_PRICE = re.compile(r"Preis:?\s*\S+\s+")
_MAX_ROOM_LENGTH = 40


def parse_beds24(email: ParsedEmail) -> EmailExtraction | None:
    """Extrahiert die Buchung zur Nummer aus dem Betreff oder liefert ``None``."""
    parsed = _parse(email)
    return parsed[0] if parsed else None


def parse_beds24_records(email: ParsedEmail) -> list[EmailExtraction]:
    """Wie ``parse_beds24``, aber eine neue Gruppenbuchung ergibt je Zimmer eine Buchung.

    Fuer eine neue Gruppe schickt Beds24 nur eine Mail. Stornierungen und
    Aenderungen kommen dagegen je Zimmer als eigene Mail - dort gilt nur die
    Nummer aus dem Betreff, sonst wuerde jede Stornierung mehrfach gezaehlt.
    Keine Beds24-Mail: leere Liste.
    """
    parsed = _parse(email)
    if parsed is None:
        return []
    primary, rooms = parsed
    if primary.email_type != "booking":
        return [primary]
    others = [
        primary.model_copy(update={"booking_reference": reference, "unit_name": unit})
        for reference, unit in rooms.items()
        if reference != primary.booking_reference
    ]
    return [primary, *others]


def _parse(email: ParsedEmail) -> tuple[EmailExtraction, dict[str, str]] | None:
    """Ergebnis zur Nummer aus dem Betreff plus Objekt je Gruppennummer."""
    subject = _FORWARD_PREFIX.sub("", " ".join(email.subject.split()))
    match = _SUBJECT.match(subject)
    if match is None:
        return None

    body = " ".join(email.body.split())
    reference = match.group("reference")
    # Der Text muss die Nummer aus dem Betreff tragen - sonst ist es nur ein
    # aehnlich klingender Betreff und keine Beds24-Benachrichtigung.
    if not re.search(rf"Buchungsnummer:?\s*{reference}\b", body):
        return None

    property_name, _, subject_room = match.group("unit").partition(":")
    property_name = property_name.strip()
    rooms = _group_rooms(body, property_name)
    # Bei Gruppen nennt der Betreff nur "Anzahl 2" - das Zimmer steht im Text.
    unit = rooms.get(reference) or _unit_name(property_name, subject_room)

    email_type = _KIND_TO_TYPE[match.group("kind").lower()]
    arrival = _body_date(body, "Check-in") or _parse_date(
        f"{match.group('day')} {match.group('month')} {match.group('year')}"
    )
    departure = _body_date(body, "Check-out")
    guest = _guest_name(body) or match.group("guest").strip() or None

    if email_type == "change":
        # Beds24 schickt bei einer Aenderung nur den neuen Stand.
        result = EmailExtraction(
            email_type="change",
            booking_reference=reference,
            guest_name=guest,
            new_arrival_date=arrival,
            new_departure_date=departure,
            new_unit_name=unit,
        )
    else:
        result = EmailExtraction(
            email_type=email_type,
            booking_reference=reference,
            guest_name=guest,
            arrival_date=arrival,
            departure_date=departure,
            unit_name=unit,
        )
    return result, rooms


def _unit_name(property_name: str, room: str) -> str:
    """Objektname aus Unterkunft und Beds24-Zimmer.

    "Ferienhaus Beispiel" + "Zimmer Nr.1"              -> "Ferienhaus Beispiel - Zimmer Nr. 1"
    "Ferienwohnung Beispiel" + "Beispiel Air BNB"      -> "Ferienwohnung Beispiel"

    Nennt der Zimmername nur die Unterkunft plus Vertriebskanal, ist es dieselbe
    Wohnung. Echte Zimmer ("Zimmer Nr. 3", "Einzelzimmer") bleiben getrennt.
    """
    property_name = property_name.strip()
    room = re.sub(r"\bNr\.\s*(\d)", r"Nr. \1", " ".join(room.split()))
    rest = normalize_unit_name(_CHANNEL_SUFFIX.sub("", room))
    if set(rest.split()) <= set(normalize_unit_name(property_name).split()):
        return property_name
    return f"{property_name} - {room}"


def _group_rooms(body: str, property_name: str) -> dict[str, str]:
    """Objekt je Buchungsnummer einer Gruppenbuchung; leer ohne "Gruppen ID".

    Der Text vor jeder Nummer wird per Slice bestimmt, nicht per ".*?" im Muster:
    Das lief bei langen Mails ohne "Gruppen ID" quadratisch - eine praeparierte
    Mail mit Beds24-Betreff haette den Abruf minutenlang blockiert.
    """
    head = body.split("Check-in")[0]
    rooms: dict[str, str] = {}
    previous_end = 0
    for match in _GROUP_ROOM.finditer(head):
        before = head[previous_end : match.start()]
        previous_end = match.end()
        prices = list(_PRICE.finditer(before))
        if prices:
            room = before[prices[-1].end() :]
        else:
            # Erstes Zimmer: steht direkt hinter dem Namen der Unterkunft.
            position = before.rfind(property_name) if property_name else -1
            room = before[position + len(property_name) :] if position >= 0 else ""
        room = room.strip()
        if room and len(room) <= _MAX_ROOM_LENGTH:
            rooms[match.group("reference")] = _unit_name(property_name, room)
    return rooms


def _guest_name(body: str) -> str | None:
    for pattern in _GUEST_PATTERNS:
        match = pattern.search(body)
        if match:
            name = match.group("name").strip()
            if name and len(name) <= _MAX_NAME_LENGTH:
                return name
    return None


def _body_date(body: str, label: str) -> date | None:
    match = re.search(rf"\b{label}:?\s+{_WEEKDAY}({_DATE})", body)
    return _parse_date(match.group(1)) if match else None


def _parse_date(raw: str) -> date | None:
    """"19 Sep 2026", "1. Mär 2026" oder "07.01.2027"."""
    text = raw.strip()
    try:
        numeric = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if numeric:
            day, month, year = (int(value) for value in numeric.groups())
            return date(year, month, day)
        named = re.fullmatch(rf"(\d{{1,2}})\.?\s+({_WORD}+)\.?\s+(\d{{4}})", text)
        if not named:
            return None
        month = _MONTHS.get(named.group(2)[:3].lower())
        if month is None:
            return None
        return date(int(named.group(3)), month, int(named.group(1)))
    except ValueError:
        return None
