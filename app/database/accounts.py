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
    RUN_ERROR,
    RUN_RUNNING,
    AgentRun,
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


class PasswordChangedError(RuntimeError):
    """Zwischen Passwortpruefung und Sitzung wurde das Passwort geaendert."""


def create_login_session(
    session: Session, *, user_id: int, hours: int, password_hash: str | None = None
) -> str:
    """Legt eine Sitzung an und gibt das Token zurueck - gespeichert wird nur sein Hash.

    ``password_hash``: der Hash, gegen den das Passwort eben geprueft wurde.
    Stimmt er nicht mehr, hat ein Reset die Anmeldung ueberholt - dann darf sie
    keine Sitzung mehr bekommen, sonst ueberlebt das alte Passwort den Wechsel.
    """
    if password_hash is not None:
        user = session.get(User, user_id)
        if user is None or user.password_hash != password_hash:
            raise PasswordChangedError()
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


def discard_codes(session: Session, *, user_id: int) -> None:
    """Alle Codes eines Nutzers entwerten - nach einem Passwortwechsel."""
    session.execute(delete(VerificationCode).where(VerificationCode.user_id == user_id))


def redeem_code(session: Session, *, user_id: int, purpose: str, code: str) -> bool:
    """Prueft den Code und verbraucht ihn bei Erfolg.

    Ein Fehlversuch wird gezaehlt; ab ``MAX_CODE_ATTEMPTS`` gilt der Code als
    verbraucht und es muss ein neuer angefordert werden.
    """
    # Zeilensperre (PostgreSQL): zwei gleichzeitige Einloesungen desselben Codes
    # laufen sonst beide durch die Pruefung, und parallele Fehlversuche
    # ueberschreiben sich gegenseitig den Zaehler. SQLite kennt die Sperre nicht
    # und ignoriert sie - dort laeuft ohnehin nur ein Prozess.
    entry = session.scalar(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user_id,
            VerificationCode.purpose == purpose,
            VerificationCode.used_at.is_(None),
        )
        .order_by(VerificationCode.id.desc())
        .with_for_update()
    )
    if entry is None or entry.expires_at < utcnow():
        return False
    if entry.attempts >= MAX_CODE_ATTEMPTS:
        return False
    if not verify_password(code.strip(), entry.code_hash):
        # Als Ausdruck in der Datenbank erhoehen, nicht gelesen-plus-eins.
        entry.attempts = VerificationCode.attempts + 1
        session.flush()
        return False
    entry.used_at = utcnow()
    session.flush()
    return True


# ---------------------------------------------------------------- Postfach einstellen


def get_mailbox(session: Session, tenant_id: int) -> Mailbox | None:
    """Das Postfach eines Mandanten. Die Oberflaeche verwaltet genau eins."""
    return session.scalar(
        select(Mailbox).where(Mailbox.tenant_id == tenant_id).order_by(Mailbox.id)
    )


def save_mailbox(
    session: Session,
    *,
    tenant_id: int,
    host: str,
    username: str,
    password: str | None,
    port: int = 993,
    folder: str = "INBOX",
    use_ssl: bool = True,
    since_date: date | None = None,
    active: bool = True,
) -> Mailbox:
    """Legt das Postfach an oder aendert es. ``password=None`` laesst es stehen.

    Aendert sich, *worauf* geschaut wird - anderer Server, anderes Konto,
    anderer Ordner, oder ein weiter zurueckliegendes Datum -, wird der
    IMAP-Cursor zurueckgesetzt. Ohne das haelt ``last_uid`` den Abruf an der
    bisherigen Stelle fest: ein zurueckgesetztes Datum wuerde die aelteren Mails
    nie erreichen. Doppelte Importe verhindert die Dublettenpruefung.
    """
    mailbox = get_mailbox(session, tenant_id)
    if mailbox is None:
        if not password:
            raise ValueError("Fuer ein neues Postfach wird das Passwort gebraucht.")
        return add_mailbox(
            session,
            tenant_id=tenant_id,
            host=host,
            username=username,
            password=password,
            port=port,
            folder=folder,
            use_ssl=use_ssl,
            since_date=since_date,
        )

    # Ein anderer Server oder ein anderes Konto verlangt das Passwort erneut:
    # sonst liesse sich das gespeicherte Passwort an einen fremden Server
    # schicken - von wem auch immer, der gerade diese Sitzung hat.
    if not password and (mailbox.host, mailbox.username) != (host, username):
        raise ValueError(
            "Fuer einen anderen Server oder Benutzernamen bitte das Passwort neu eingeben."
        )
    rescan = needs_rescan(mailbox, host=host, username=username, folder=folder, since_date=since_date)
    mailbox.host = host
    mailbox.username = username
    mailbox.port = port
    mailbox.folder = folder
    mailbox.use_ssl = use_ssl
    mailbox.since_date = since_date
    mailbox.active = active
    if password:
        mailbox.password_encrypted = encrypt_secret(password)
        # Neues Passwort: der alte Fehlerstand sagt nichts mehr aus.
        mailbox.status = "unknown"
        mailbox.status_message = None
    if rescan:
        mailbox.last_uid = None
        mailbox.uid_validity = None
        mailbox.retry_uid = None
        mailbox.retry_count = 0
    session.flush()
    return mailbox


def needs_rescan(
    mailbox: Mailbox,
    *,
    host: str,
    username: str,
    folder: str,
    since_date: date | None,
) -> bool:
    """Muss der Abruf von vorn anfangen?

    Bei einem anderen Postfach oder Ordner sagt der Cursor nichts mehr aus. Beim
    Datum zaehlt nur der Weg zurueck: nach vorn schraenkt ``since_date`` die
    Suche ohnehin ein, zurueck liegen die gesuchten Mails vor dem Cursor.
    """
    if (mailbox.host, mailbox.username, mailbox.folder) != (host, username, folder):
        return True
    alt, neu = mailbox.since_date, since_date
    if neu is None:
        return alt is not None
    return alt is not None and neu < alt


def record_check(
    session: Session, mailbox_id: int, *, status: str, message: str | None
) -> None:
    """Ergebnis des Verbindungstests am Postfach vermerken."""
    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None:
        return
    mailbox.status = status
    mailbox.status_message = message
    mailbox.last_check_at = datetime.now()
    session.flush()


# ---------------------------------------------------------------- Laeufe des Agenten


def start_run(
    session: Session, *, tenant_id: int, mailbox_id: int | None, kind: str
) -> int:
    run = AgentRun(
        tenant_id=tenant_id,
        mailbox_id=mailbox_id,
        kind=kind,
        status=RUN_RUNNING,
        started_at=datetime.now(),
    )
    session.add(run)
    session.flush()
    return run.id


def finish_run(
    session: Session,
    run_id: int,
    *,
    status: str,
    message: str | None = None,
    imported: int = 0,
    skipped: int = 0,
    bookings: int = 0,
    cancellations: int = 0,
    changes: int = 0,
    failed: int = 0,
) -> None:
    run = session.get(AgentRun, run_id)
    if run is None:
        return
    run.status = status
    run.message = message
    run.finished_at = datetime.now()
    run.imported = imported
    run.skipped = skipped
    run.bookings = bookings
    run.cancellations = cancellations
    run.changes = changes
    run.failed = failed
    session.flush()


def recent_runs(session: Session, *, tenant_id: int, limit: int = 20) -> list[AgentRun]:
    return list(
        session.scalars(
            select(AgentRun)
            .where(AgentRun.tenant_id == tenant_id)
            .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
            .limit(limit)
        )
    )


def abandon_stale_runs(session: Session, *, older_than: timedelta) -> int:
    """Laeufe, die nie fertig wurden (Absturz, Neustart), nicht ewig als laufend zeigen."""
    stale = list(
        session.scalars(
            select(AgentRun).where(
                AgentRun.status == RUN_RUNNING,
                AgentRun.started_at < datetime.now() - older_than,
            )
        )
    )
    for run in stale:
        run.status = RUN_ERROR
        run.message = "Abgebrochen - der Dienst wurde zwischendurch beendet."
        run.finished_at = datetime.now()
    session.flush()
    return len(stale)
