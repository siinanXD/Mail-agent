"""Erkennung und Normalisierung von Objekten (Ferienwohnung / Wohnung / Haus).

Die Objekte werden vollautomatisch aus den E-Mails angelegt. Damit dabei keine
Dubletten entstehen, wird jeder Name auf einen Schluessel normalisiert:

    "Ferienwohnung Seeblick"  ->  "seeblick"
    "FeWo Seeblick"           ->  "seeblick"
    "Fewo  Seeblick!"         ->  "seeblick"
    "Haus Bergblick (OG)"     ->  "bergblick og"

Nur wenn der Schluessel neu ist, entsteht ein neues Objekt.
"""

from __future__ import annotations

import re
import unicodedata

#: Gattungsbegriffe, die keinen Namen darstellen und deshalb entfallen.
_TYPE_WORDS = {
    "ferienwohnung",
    "ferienwohnungen",
    "fewo",
    "fw",
    "wohnung",
    "whg",
    "apartment",
    "appartement",
    "apartments",
    "haus",
    "ferienhaus",
    "studio",
    "objekt",
    "unterkunft",
}

_UMLAUTS = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"}
)


def normalize_unit_name(name: str) -> str:
    """Erzeugt den Vergleichsschluessel eines Objektnamens."""
    text = name.translate(_UMLAUTS).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()

    words = [word for word in text.split() if word not in _TYPE_WORDS]
    if not words:
        # Nur ein Gattungsbegriff ("Ferienwohnung") - dann ist der eben der Name.
        words = text.split()
    return " ".join(words)


def display_name(name: str) -> str:
    """Raeumt den Anzeigenamen auf, ohne ihn inhaltlich zu veraendern."""
    return re.sub(r"\s+", " ", name).strip(" .,;:-")
