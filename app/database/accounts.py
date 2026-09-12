"""Mandanten, Nutzer und Postfaecher - die Verwaltungsebene.

Diese Tabellen stehen bewusst ausserhalb der Row-Level-Security: Anmeldung und
Watcher muessen mandantenuebergreifend lesen koennen (zu welchem Mandanten
gehoert ein Nutzer, welche Postfaecher gibt es ueberhaupt). Mandantendaten -
Mails, Buchungen, Objekte - liegen in ``repositories.py`` und sind geschuetzt.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.crypto import encrypt_secret, hash_password, verify_password
from app.database.models import (
    LoginSession,
    Mailbox,
    Tenant,
    User,
    VerificationCode,
    utcnow,
)

#: Gueltigkeit eines Einmalcodes. Lang genug fuer eine Mail, die ein paar
#: Minuten braucht - kurz genug, dass ein abgefangener Code schnell wertlos ist.
CODE_TTL = timedelta(minutes=15)
#: So oft darf ein Code falsch eingegeben werden, dann ist er verbraucht.
#: Ohne Grenze waeren sechs Ziffern in Minuten durchprobiert.
MAX_CODE_ATTEMPTS = 5

# ---------------------------------------------------------------- Mandanten


def create_tenant(session: Session, *, name: str, slug: str) -> Tenant:
    tenant = Tenant(name=name, slug=slug, active=True)
    session.add(tenant)
    session.flush()
    return tenant


def get_tenant_by_slug(session: Session, slug: str) -> Tenant | None:
    return session.scalar(select(Tenant).where(Tenant.slug == slug))


def list_tenants(session: Session) -> list[Tenant]:
    return list(session.scalars(select(Tenant).order_by(Tenant.id)))


# ---------------------------------------------------------------- Nutzer


def create_user(
    session: Session,
    *,
    tenant_id: int,
    email: str,
    password: str,
    verified: bool = True,
) -> User:
    """Legt einen Nutzer an.

    ``verified=True`` ist der Standard fuer die Verwaltung (CLI, Bootstrap):
    dort hat ein Mensch die Adresse eingetragen. Die Selbstregistrierung setzt
    ``verified=False`` - dieser Nutzer kommt erst nach dem Code aus der Mail rein.
    """
    user = User(
        tenant_id=tenant_id,
        email=email.strip().lower(),
        password_hash=hash_password(password),
        active=True,
        verified_at=utcnow() if verified else None,
    )
    session.add(user)
    session.flush()
    return user


def set_password(session: Session, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    session.flush()


def mark_verified(session: Session, user: User) -> None:
    if user.verified_at is None:
        user.verified_at = utcnow()
        session.flush()


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.scalar(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )


def count_users(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(User)) or 0


def list_users(session: Session, tenant_id: int | None = None) -> list[User]:
    stmt = select(User).order_by(User.tenant_id, User.email)
    if tenant_id is not None:
        stmt = stmt.where(User.tenant_id == tenant_id)
    return list(session.scalars(stmt))


# ---------------------------------------------------------------- Postfaecher


def add_mailbox(
    session: Session,
    *,
    tenant_id: int,
    host: str,
    username: str,
    password: str,
    port: int = 993,
    folder: str = "INBOX",
    use_ssl: bool = True,
    since_date: date | None = None,
) -> Mailbox:
    """Legt ein Postfach an. Das Passwort wird vor dem Speichern verschluesselt."""
    mailbox = Mailbox(
        tenant_id=tenant_id,
        host=host,
        port=port,
        username=username,
        password_encrypted=encrypt_secret(password),
        folder=folder,
        use_ssl=use_ssl,
        since_date=since_date,
        active=True,
    )
    session.add(mailbox)
    session.flush()
    return mailbox


def active_mailboxes(session: Session, tenant_id: int | None = None) -> list[Mailbox]:
    """Aktive Postfaecher aktiver Mandanten."""
    stmt = (
        select(Mailbox)
        .join(Tenant, Mailbox.tenant_id == Tenant.id)
        .where(Mailbox.active.is_(True), Tenant.active.is_(True))
        .order_by(Mailbox.tenant_id, Mailbox.id)
    )
    if tenant_id is not None:
        stmt = stmt.where(Mailbox.tenant_id == tenant_id)
    return list(session.scalars(stmt))


def mark_polled(session: Session, mailbox_id: int, error: str | None) -> None:
    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        return
    mailbox.last_polled_at = datetime.now()
    mailbox.last_error = error
    session.flush()


def save_cursor(
    session: Session,
    mailbox_id: int,
    *,
    uid_validity: int | None,
    last_uid: int | None,
    retry_uid: int | None = None,
    retry_count: int = 0,
) -> None:
    """Merkt sich, bis zu welcher UID das Postfach verarbeitet ist.

    ``retry_uid``/``retry_count``: die erste fehlgeschlagene UID, vor der der
    Cursor steht, und wie oft sie schon versucht wurde.
    """
    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        return
    mailbox.uid_validity = uid_validity
    mailbox.last_uid = last_uid
    mailbox.retry_uid = retry_uid
    mailbox.retry_count = retry_count
    session.flush()


# ---------------------------------------------------------------- Selbstregistrierung

_SLUG_ALLOWED = re.compile(r"[^a-z0-9-]+")


def slugify(value: str) -> str:
    """Kurzname aus einem Anzeigenamen - Umlaute inklusive."""
    lowered = value.strip().lower()
    for umlaut, replacement in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        lowered = lowered.replace(umlaut, replacement)
    slug = _SLUG_ALLOWED.sub("-", lowered).strip("-")[:56]
    return slug or "kunde"


def unique_slug(session: Session, base: str) -> str:
    """Haengt -2, -3 ... an, bis der Kurzname frei ist."""
    slug = slugify(base)
    candidate, suffix = slug, 1
    while get_tenant_by_slug(session, candidate) is not None:
        suffix += 1
        candidate = f"{slug}-{suffix}"
    return candidate


def create_tenant_with_owner(
    session: Session, *, name: str, email: str, password: str
) -> User:
    """Neuer Mandant samt erstem (noch unbestaetigtem) Nutzer.

    Jede Registrierung bekommt einen eigenen Mandanten: eigene Mails, eigene
    Buchungen, eigenes Postfach. Weitere Nutzer desselben Kunden legt spaeter
    die Verwaltung in diesem Mandanten an.
    """
    tenant = create_tenant(session, name=name, slug=unique_slug(session, name))
    return create_user(
        session,
        tenant_id=tenant.id,
        email=email,
        password=password,
        verified=False,
    )


# ---------------------------------------------------------------- Sitzungen


def _token_hash(token: str) -> str:
    """SHA-256 genuegt: das Token ist 256 Bit Zufall, nicht zu erraten."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_login_session(session: Session, *, user_id: int, hours: int) -> str:
    """Legt eine Sitzung an und gibt das Token zurueck - gespeichert wird nur sein Hash."""
    _purge_expired_sessions(session)
    token = secrets.token_urlsafe(32)
    session.add(
        LoginSession(
            user_id=user_id,
            token_hash=_token_hash(token),
            expires_at=utcnow() + timedelta(hours=hours),
        )
    )
    session.flush()
    return token


def get_session_user(session: Session, token: str) -> User | None:
    """Nutzer zu einem Token - None, wenn es abgelaufen oder unbekannt ist."""
    entry = session.scalar(
        select(LoginSession).where(LoginSession.token_hash == _token_hash(token))
    )
    if entry is None:
        return None
    if entry.expires_at < utcnow():
        session.delete(entry)
        session.flush()
        return None
    return entry.user


def end_login_session(session: Session, token: str) -> None:
    session.execute(
        delete(LoginSession).where(LoginSession.token_hash == _token_hash(token))
    )


def end_all_sessions(session: Session, user_id: int) -> None:
    """Nach einem Passwortwechsel: alle offenen Sitzungen des Nutzers beenden."""
    session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))


def _purge_expired_sessions(session: Session) -> None:
    session.execute(delete(LoginSession).where(LoginSession.expires_at < utcnow()))


# ---------------------------------------------------------------- Einmalcodes


def issue_code(session: Session, *, user_id: int, purpose: str) -> str:
    """Neuer Code fuer Nutzer und Zweck. Aeltere Codes dazu verfallen."""
    session.execute(
        delete(VerificationCode).where(
            VerificationCode.user_id == user_id, VerificationCode.purpose == purpose
        )
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(
        VerificationCode(
            user_id=user_id,
            purpose=purpose,
            code_hash=hash_password(code),
            expires_at=utcnow() + CODE_TTL,
        )
    )
    session.flush()
    return code


def redeem_code(session: Session, *, user_id: int, purpose: str, code: str) -> bool:
    """Prueft den Code und verbraucht ihn bei Erfolg.

    Ein Fehlversuch wird gezaehlt; ab ``MAX_CODE_ATTEMPTS`` gilt der Code als
    verbraucht und es muss ein neuer angefordert werden.
    """
    entry = session.scalar(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user_id,
            VerificationCode.purpose == purpose,
            VerificationCode.used_at.is_(None),
        )
        .order_by(VerificationCode.id.desc())
    )
    if entry is None or entry.expires_at < utcnow():
        return False
    if entry.attempts >= MAX_CODE_ATTEMPTS:
        return False
    if not verify_password(code.strip(), entry.code_hash):
        entry.attempts += 1
        session.flush()
        return False
    entry.used_at = utcnow()
    session.flush()
    return True
