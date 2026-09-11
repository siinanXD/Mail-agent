"""Mandanten, Nutzer und Postfaecher - die Verwaltungsebene.

Diese Tabellen stehen bewusst ausserhalb der Row-Level-Security: Anmeldung und
Watcher muessen mandantenuebergreifend lesen koennen (zu welchem Mandanten
gehoert ein Nutzer, welche Postfaecher gibt es ueberhaupt). Mandantendaten -
Mails, Buchungen, Objekte - liegen in ``repositories.py`` und sind geschuetzt.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.crypto import encrypt_secret, hash_password
from app.database.models import Mailbox, Tenant, User

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


def create_user(session: Session, *, tenant_id: int, email: str, password: str) -> User:
    user = User(
        tenant_id=tenant_id,
        email=email.strip().lower(),
        password_hash=hash_password(password),
        active=True,
    )
    session.add(user)
    session.flush()
    return user


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
    session: Session, mailbox_id: int, *, uid_validity: int | None, last_uid: int | None
) -> None:
    """Merkt sich, bis zu welcher UID das Postfach verarbeitet ist."""
    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        return
    mailbox.uid_validity = uid_validity
    mailbox.last_uid = last_uid
    session.flush()
