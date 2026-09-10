"""Passwoerter und Geheimnisse.

Zwei verschiedene Faelle, zwei verschiedene Werkzeuge:

* **Nutzerpasswoerter** muessen nie wieder im Klartext gebraucht werden -> Hash
  mit scrypt (Standardbibliothek, speicherintensiv, gegen GPU-Angriffe gedacht).
* **IMAP-Passwoerter** muss der Watcher an den Mailserver schicken -> sie werden
  umkehrbar verschluesselt (Fernet: AES-128-CBC + HMAC-SHA256). Der Schluessel
  kommt aus ENCRYPTION_KEY und liegt nie in der Datenbank.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32


class EncryptionNotConfiguredError(RuntimeError):
    """ENCRYPTION_KEY fehlt - Postfach-Passwoerter koennen nicht gespeichert werden."""


class DecryptionError(RuntimeError):
    """Das gespeicherte Geheimnis passt nicht zum aktuellen Schluessel."""


# ---------------------------------------------------------------- Nutzerpasswoerter


def hash_password(password: str) -> str:
    """Erzeugt ``scrypt$N$r$p$salt$hash`` - alle Parameter stehen mit drin."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LEN,
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False

    expected = base64.b64decode(hash_b64)
    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=base64.b64decode(salt_b64),
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=len(expected),
    )
    # Vergleich in konstanter Zeit - die Laufzeit verraet nichts ueber den Hash.
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------- Postfach-Geheimnisse


def generate_key() -> str:
    """Neuer Schluessel fuer ENCRYPTION_KEY."""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise EncryptionNotConfiguredError(
            "ENCRYPTION_KEY ist nicht gesetzt. Erzeugen mit: "
            "python -m app.admin generate-key"
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as error:
        raise EncryptionNotConfiguredError(
            "ENCRYPTION_KEY ist kein gueltiger Fernet-Schluessel."
        ) from error


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode("utf-8")
    except InvalidToken as error:
        raise DecryptionError(
            "Postfach-Passwort laesst sich nicht entschluesseln - wurde "
            "ENCRYPTION_KEY geaendert?"
        ) from error
