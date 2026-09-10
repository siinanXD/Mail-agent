"""Belege: wo im E-Mail-Text steht der extrahierte Wert?

Die Extraktion laeuft ueber ein LLM und liefert nur das Ergebnis, keine
Textstellen. Statt Fundstellen zu erfinden, wird hier nachtraeglich im Original
gesucht: Wird ein Wert woertlich gefunden, gibt es die Fundstelle; wird er nicht
gefunden, steht er ausdruecklich als "abgeleitet" da.

Das ist ehrlicher als eine Hervorhebung, die so tut, als haette das Modell genau
diese Stelle gelesen.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from app.units import normalize_unit_name


@dataclass
class Match:
    """Eine Fundstelle im Mailtext."""

    start: int
    end: int


@dataclass
class Evidence:
    """Ein extrahiertes Feld und sein Beleg im Originaltext."""

    field: str
    label: str
    value: str
    matches: list[Match] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.matches)


def date_variants(value: date) -> list[str]:
    """Schreibweisen, in denen ein Datum in einer Mail stehen kann."""
    return [
        value.isoformat(),
        value.strftime("%d.%m.%Y"),
        f"{value.day}.{value.month}.{value.year}",
        value.strftime("%d.%m.%y"),
        f"{value.day}.{value.month}.",
    ]


def find_matches(haystack: str, needle: str) -> list[Match]:
    """Alle Vorkommen von ``needle``, unabhaengig von Gross-/Kleinschreibung."""
    if not needle or not haystack:
        return []
    matches: list[Match] = []
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    for hit in pattern.finditer(haystack):
        matches.append(Match(hit.start(), hit.end()))
    return matches


def _first_hit(text: str, candidates: list[str]) -> list[Match]:
    """Nimmt die erste Schreibweise, die im Text vorkommt."""
    for candidate in candidates:
        matches = find_matches(text, candidate)
        if matches:
            return matches
    return []


def _name_matches(text: str, name: str) -> list[Match]:
    """Ganzer Name, sonst der laengste Namensteil (z.B. Nachname)."""
    matches = find_matches(text, name)
    if matches:
        return matches
    parts = sorted((p for p in name.split() if len(p) > 2), key=len, reverse=True)
    return _first_hit(text, parts)


def _unit_matches(text: str, unit_name: str) -> list[Match]:
    """Objektname; sonst der Kern des Namens ohne Gattungswort."""
    matches = find_matches(text, unit_name)
    if matches:
        return matches
    core = normalize_unit_name(unit_name)
    if not core:
        return []
    # "seeblick" findet auch "FeWo Seeblick" und "Ferienwohnung Seeblick".
    return _first_hit(text, [core, *core.split()])


def collect_evidence(
    *, subject: str, body: str, booking=None, cancellation=None, changes=None
) -> list[Evidence]:
    """Baut die Beleg-Liste fuer eine E-Mail und ihre extrahierten Daten."""
    text = f"{subject}\n{body}"
    items: list[Evidence] = []

    def add(field_name: str, label: str, value, matches: list[Match]) -> None:
        if value in (None, ""):
            return
        items.append(Evidence(field_name, label, str(value), matches))

    if booking is not None:
        add(
            "booking_reference",
            "Buchungsnummer",
            booking.booking_reference,
            find_matches(text, booking.booking_reference),
        )
        add("guest_name", "Gast", booking.guest_name, _name_matches(text, booking.guest_name))
        if booking.unit is not None:
            add("unit", "Objekt", booking.unit.name, _unit_matches(text, booking.unit.name))
        if booking.arrival_date:
            add(
                "arrival_date",
                "Anreise",
                booking.arrival_date.isoformat(),
                _first_hit(text, date_variants(booking.arrival_date)),
            )
        if booking.departure_date:
            add(
                "departure_date",
                "Abreise",
                booking.departure_date.isoformat(),
                _first_hit(text, date_variants(booking.departure_date)),
            )

    if cancellation is not None:
        linked = cancellation.booking
        if linked is not None:
            add(
                "booking_reference",
                "Buchungsnummer",
                linked.booking_reference,
                find_matches(text, linked.booking_reference),
            )
            add("guest_name", "Gast", linked.guest_name, _name_matches(text, linked.guest_name))
            if linked.unit is not None:
                add("unit", "Objekt", linked.unit.name, _unit_matches(text, linked.unit.name))
        add(
            "cancelled_at",
            "Storniert am",
            cancellation.cancelled_at.date().isoformat(),
            _first_hit(text, date_variants(cancellation.cancelled_at.date())),
        )
        if cancellation.reason:
            add(
                "reason",
                "Grund",
                cancellation.reason,
                _reason_matches(text, cancellation.reason),
            )

    for change in changes or []:
        label = {
            "arrival_date": "Neue Anreise",
            "departure_date": "Neue Abreise",
            "unit": "Neues Objekt",
        }.get(change.field, change.field)
        matches: list[Match] = []
        if change.new_value:
            parsed = _try_date(change.new_value)
            matches = (
                _first_hit(text, date_variants(parsed))
                if parsed
                else find_matches(text, change.new_value)
            )
        add(f"change_{change.field}", label, change.new_value, matches)
        if change.old_value:
            parsed_old = _try_date(change.old_value)
            add(
                f"change_{change.field}_alt",
                f"Bisher ({label.lower()})",
                change.old_value,
                _first_hit(text, date_variants(parsed_old))
                if parsed_old
                else find_matches(text, change.old_value),
            )

    return items


def _try_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _reason_matches(text: str, reason: str) -> list[Match]:
    """Der Grund ist meist zusammengefasst ("Flugausfall") - Wortstamm suchen."""
    matches = find_matches(text, reason)
    if matches:
        return matches

    stems = {
        "flugausfall": ["flugausfall", "flug"],
        "krankheit": ["krank"],
        "berufliche terminverschiebung": ["beruflich", "termin"],
    }.get(_fold(reason), [])
    if not stems:
        # Laengstes Wort des Grundes als letzter Versuch.
        stems = sorted(
            (w for w in re.findall(r"\w{5,}", reason.lower())), key=len, reverse=True
        )
    return _first_hit(text, stems)


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


def merge_matches(matches: list[Match]) -> list[Match]:
    """Ueberlappende Fundstellen zusammenfassen - fuer die Hervorhebung."""
    if not matches:
        return []
    ordered = sorted(matches, key=lambda m: (m.start, m.end))
    merged = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if current.start <= last.end:
            merged[-1] = Match(last.start, max(last.end, current.end))
        else:
            merged.append(current)
    return merged
