"""Verwaltung von Mandanten, Nutzern und Postfaechern.

    python -m app.admin generate-key
    python -m app.admin create-tenant "Ferienwohnungen Mueller" mueller
    python -m app.admin create-user mueller anna@mueller.de
    python -m app.admin add-mailbox mueller imap.example.de anna@mueller.de --since 2026-09-01
    python -m app.admin list

Passwoerter werden abgefragt statt als Argument uebergeben, damit sie nicht in
der Shell-History landen. Fuer Skripte gibt es trotzdem ``--password``.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import re
import sys
from datetime import date

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.crypto import EncryptionNotConfiguredError, generate_key
from app.database import accounts
from app.database.connection import session_scope

logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
DEFAULT_TENANT_SLUG = "standard"


# ---------------------------------------------------------------- Start-Bootstrap


def bootstrap() -> None:
    """Richtet beim Start das Noetigste ein - und macht danach nichts mehr.

    * Gibt es noch keinen Nutzer und sind BOOTSTRAP_ADMIN_* gesetzt, wird ein
      Admin im Standard-Mandanten angelegt.
    * Sind IMAP_* in der .env gesetzt und hat der Standard-Mandant noch kein
      Postfach, wird es verschluesselt uebernommen (Umstieg vom Einzelbetrieb).
    """
    settings = get_settings()
    with session_scope() as session:
        tenant = accounts.get_tenant_by_slug(session, DEFAULT_TENANT_SLUG)
        if tenant is None:
            tenant = accounts.create_tenant(
                session, name="Standard", slug=DEFAULT_TENANT_SLUG
            )

        wants_admin = settings.bootstrap_admin_email and settings.bootstrap_admin_password
        if wants_admin and accounts.count_users(session) == 0:
            accounts.create_user(
                session,
                tenant_id=tenant.id,
                email=settings.bootstrap_admin_email,
                password=settings.bootstrap_admin_password,
            )
            logger.info(
                "Erster Nutzer %s im Mandanten '%s' angelegt.",
                settings.bootstrap_admin_email,
                tenant.slug,
            )
        elif accounts.count_users(session) == 0:
            logger.warning(
                "Es gibt noch keinen Nutzer - Anmeldung unmoeglich. Anlegen mit "
                "'python -m app.admin create-user standard <email>' oder "
                "BOOTSTRAP_ADMIN_EMAIL/BOOTSTRAP_ADMIN_PASSWORD setzen."
            )

        if settings.imap_configured and not accounts.active_mailboxes(
            session, tenant_id=tenant.id
        ):
            try:
                accounts.add_mailbox(
                    session,
                    tenant_id=tenant.id,
                    host=settings.imap_host,
                    port=settings.imap_port,
                    username=settings.imap_user,
                    password=settings.imap_password,
                    folder=settings.imap_folder,
                    use_ssl=settings.imap_ssl,
                    since_date=date.fromisoformat(settings.imap_since)
                    if settings.imap_since
                    else None,
                )
                logger.info("Postfach aus der .env in Mandant '%s' uebernommen.", tenant.slug)
            except EncryptionNotConfiguredError:
                logger.warning(
                    "IMAP_* ist gesetzt, aber ENCRYPTION_KEY fehlt - das Postfach "
                    "wird nicht uebernommen, weil das Passwort sonst im Klartext laege."
                )


# ---------------------------------------------------------------- CLI


def _ask_password(confirm: bool) -> str:
    password = getpass.getpass("Passwort: ")
    if confirm and getpass.getpass("Wiederholen: ") != password:
        raise SystemExit("Passwoerter stimmen nicht ueberein.")
    if len(password) < 10:
        raise SystemExit("Bitte mindestens 10 Zeichen.")
    return password


def _tenant_or_exit(session, slug: str):
    tenant = accounts.get_tenant_by_slug(session, slug)
    if tenant is None:
        raise SystemExit(f"Mandant '{slug}' gibt es nicht. Anlegen mit create-tenant.")
    return tenant


def cmd_generate_key(_: argparse.Namespace) -> None:
    print(generate_key())
    print(
        "\nIn .env eintragen als ENCRYPTION_KEY=...\n"
        "Nicht mehr aendern, sobald Postfaecher angelegt sind - sonst lassen\n"
        "sich die gespeicherten Passwoerter nicht mehr entschluesseln.",
        file=sys.stderr,
    )


def cmd_create_tenant(args: argparse.Namespace) -> None:
    if not SLUG_PATTERN.fullmatch(args.slug):
        raise SystemExit("Kurzname: Kleinbuchstaben, Ziffern, Bindestrich (2-63 Zeichen).")
    try:
        with session_scope() as session:
            tenant = accounts.create_tenant(session, name=args.name, slug=args.slug)
            print(f"Mandant angelegt: #{tenant.id} {tenant.name} ({tenant.slug})")
    except IntegrityError:
        raise SystemExit(f"Kurzname '{args.slug}' ist schon vergeben.") from None


def cmd_create_user(args: argparse.Namespace) -> None:
    password = args.password or _ask_password(confirm=True)
    try:
        with session_scope() as session:
            tenant = _tenant_or_exit(session, args.tenant)
            user = accounts.create_user(
                session, tenant_id=tenant.id, email=args.email, password=password
            )
            print(f"Nutzer angelegt: {user.email} -> Mandant {tenant.slug}")
    except IntegrityError:
        raise SystemExit(f"Die Adresse {args.email} ist schon vergeben.") from None


def cmd_add_mailbox(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass("IMAP-Passwort: ")
    since = date.fromisoformat(args.since) if args.since else None
    try:
        with session_scope() as session:
            tenant = _tenant_or_exit(session, args.tenant)
            mailbox = accounts.add_mailbox(
                session,
                tenant_id=tenant.id,
                host=args.host,
                port=args.port,
                username=args.username,
                password=password,
                folder=args.folder,
                use_ssl=not args.no_ssl,
                since_date=since,
            )
            print(
                f"Postfach #{mailbox.id} angelegt: {mailbox.username}@{mailbox.host}"
                f"/{mailbox.folder} -> Mandant {tenant.slug}"
            )
    except EncryptionNotConfiguredError as error:
        raise SystemExit(str(error)) from None


def cmd_list(_: argparse.Namespace) -> None:
    with session_scope() as session:
        for tenant in accounts.list_tenants(session):
            status = "" if tenant.active else " (inaktiv)"
            print(f"#{tenant.id} {tenant.name} [{tenant.slug}]{status}")
            for user in accounts.list_users(session, tenant_id=tenant.id):
                print(f"    Nutzer   {user.email}{'' if user.active else ' (inaktiv)'}")
            for mailbox in accounts.active_mailboxes(session, tenant_id=tenant.id):
                last = mailbox.last_polled_at.strftime("%d.%m. %H:%M") if mailbox.last_polled_at else "nie"
                error = f"  FEHLER: {mailbox.last_error}" if mailbox.last_error else ""
                print(
                    f"    Postfach {mailbox.username}@{mailbox.host}/{mailbox.folder}"
                    f"  zuletzt: {last}{error}"
                )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-key", help="Schluessel fuer ENCRYPTION_KEY erzeugen").set_defaults(
        func=cmd_generate_key
    )

    p = sub.add_parser("create-tenant", help="Mandant anlegen")
    p.add_argument("name")
    p.add_argument("slug", help="Kurzname, z.B. mueller")
    p.set_defaults(func=cmd_create_tenant)

    p = sub.add_parser("create-user", help="Nutzer in einem Mandanten anlegen")
    p.add_argument("tenant", help="Kurzname des Mandanten")
    p.add_argument("email")
    p.add_argument("--password", help="nur fuer Skripte - sonst wird gefragt")
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("add-mailbox", help="IMAP-Postfach fuer einen Mandanten hinterlegen")
    p.add_argument("tenant")
    p.add_argument("host")
    p.add_argument("username")
    p.add_argument("--port", type=int, default=993)
    p.add_argument("--folder", default="INBOX")
    p.add_argument("--no-ssl", action="store_true")
    p.add_argument("--since", help="nur Mails ab Datum, z.B. 2026-09-01")
    p.add_argument("--password", help="nur fuer Skripte - sonst wird gefragt")
    p.set_defaults(func=cmd_add_mailbox)

    sub.add_parser("list", help="Mandanten, Nutzer und Postfaecher anzeigen").set_defaults(
        func=cmd_list
    )

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
