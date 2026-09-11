"""Telefonnummern der Mitarbeiter ins Format E.164 bringen ("+491711234567").

WhatsApp adressiert ausschliesslich ueber E.164. Eingegeben wird aber, wie man
Nummern eben schreibt: "0171 / 123 45 67", "+49 (0)171-1234567", "0049171...".
"""

from __future__ import annotations

import re

from app.config import get_settings

_E164 = re.compile(r"\+[1-9]\d{6,14}")
_SEPARATORS = re.compile(r"[\s\-/().]")


class InvalidPhoneError(ValueError):
    """Die Eingabe laesst sich nicht als Telefonnummer lesen."""


def normalize_phone(raw: str, *, default_country_code: str | None = None) -> str:
    """E.164-Nummer aus einer Eingabe. Nationale Nummern ("0171...") bekommen die
    Standard-Vorwahl (``PHONE_DEFAULT_COUNTRY_CODE``, sonst 49)."""
    country = (default_country_code or get_settings().phone_default_country_code).lstrip("+")
    # "+49 (0) 171" - die (0) gehoert zur nationalen Schreibweise, nicht zur Nummer.
    cleaned = _SEPARATORS.sub("", (raw or "").replace("(0)", ""))

    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    elif cleaned.startswith("0"):
        cleaned = f"+{country}{cleaned[1:]}"

    if not _E164.fullmatch(cleaned):
        raise InvalidPhoneError(
            "Telefonnummer nicht erkannt. Bitte z.B. als 0171 1234567 oder +49 171 1234567 angeben."
        )
    return cleaned
