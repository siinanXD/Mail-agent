"""Ausgehende Mails: die Einmalcodes fuer Bestaetigung und Passwort-Reset.

Eingehende Mails holt ``app.email`` per IMAP - das hier ist die Gegenrichtung
und bewusst klein gehalten: ein SMTP-Konto aus der .env, zwei Textbausteine.

Ohne SMTP_HOST wird nichts verschickt, sondern der Code ins Log geschrieben.
Das ist fuer die Entwicklung gedacht (man kommt ohne Mailserver durch den
Ablauf) und im Log deshalb eine Warnung - im Betrieb darf es das nicht geben.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Laenger als ein Webrequest warten darf - die Registrierung haengt daran.
SMTP_TIMEOUT_SECONDS = 15


class MailSendError(RuntimeError):
    """Die Mail konnte nicht zugestellt werden."""


def send_mail(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    if not settings.smtp_configured:
        logger.warning(
            "SMTP ist nicht konfiguriert - Mail an %s wird nicht verschickt.\n"
            "--- %s ---\n%s",
            to,
            subject,
            body,
        )
        return

    message = EmailMessage()
    message["From"] = settings.mail_sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        with _connect(settings) as server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        logger.exception("Mail an %s konnte nicht verschickt werden", to)
        raise MailSendError(
            "Die E-Mail konnte nicht verschickt werden. Bitte spaeter erneut versuchen."
        ) from error
    logger.info("Mail an %s verschickt: %s", to, subject)


def _connect(settings) -> smtplib.SMTP:
    if settings.smtp_starttls:
        server = smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
        )
        server.starttls()
        return server
    return smtplib.SMTP_SSL(
        settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
    )


# ---------------------------------------------------------------- Textbausteine


def send_signup_code(to: str, code: str, minutes: int) -> None:
    send_mail(
        to,
        "Mail Agent: E-Mail bestaetigen",
        f"Willkommen beim Mail Agent.\n\n"
        f"Dein Bestaetigungscode lautet: {code}\n\n"
        f"Er gilt {minutes} Minuten. Gib ihn im Browser ein, um dein Konto "
        f"freizuschalten.\n\n"
        f"Wenn du dich nicht registriert hast, kannst du diese Mail ignorieren.",
    )


def send_reset_code(to: str, code: str, minutes: int) -> None:
    send_mail(
        to,
        "Mail Agent: Passwort zuruecksetzen",
        f"Fuer dein Konto wurde ein neues Passwort angefordert.\n\n"
        f"Dein Code lautet: {code}\n\n"
        f"Er gilt {minutes} Minuten.\n\n"
        f"Wenn du das nicht warst, musst du nichts tun - dein Passwort bleibt "
        f"unveraendert, solange niemand diesen Code hat.",
    )


def send_already_registered(to: str) -> None:
    """Antwort auf eine Registrierung mit einer Adresse, die es schon gibt.

    Der Registrierung selbst sieht man nicht an, ob die Adresse bekannt war -
    sonst koennte man fremde Konten erraten. Der Hinweis geht deshalb an den
    Postfachinhaber, nicht an den Browser.
    """
    send_mail(
        to,
        "Mail Agent: Registrierung mit deiner Adresse",
        "Jemand hat versucht, sich mit deiner Adresse beim Mail Agent zu "
        "registrieren. Ein Konto gibt es dafuer bereits.\n\n"
        "Warst du das und hast dein Passwort vergessen, nutze im Anmeldefenster "
        "'Passwort vergessen'. Sonst ist nichts zu tun.",
    )
