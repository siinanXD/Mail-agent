"""Zentrale Konfiguration, aus Umgebungsvariablen bzw. .env geladen."""

import logging
from datetime import time
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM ---
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # --- Datenbank ---
    #: Verbindung der Anwendung. Darf kein Superuser sein, sonst greift RLS nicht.
    #: Ohne Passwort im Default - das kommt immer aus der Umgebung.
    #: 127.0.0.1 statt localhost: Die Datenbank lauscht nur auf IPv4, "localhost"
    #: versucht zuerst IPv6 und haengt dort bis zum Timeout.
    database_url: str = "postgresql+psycopg://mailagent_app@127.0.0.1:5432/mailagent"
    #: Owner-Verbindung fuer Migrationen und die Verwaltung der App-Rolle.
    #: Leer = DATABASE_URL (dann ist RLS wirkungslos, siehe Startwarnung).
    migration_database_url: str = ""

    # --- Langfuse (optional) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- IMAP-Postfach ---
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_ssl: bool = True
    #: Nur Mails ab diesem Datum holen (ISO, leer = alle im Ordner).
    imap_since: str = ""

    # --- Automatischer Abruf ---
    watch_enabled: bool = True
    #: Uhrzeiten (lokale Serverzeit), zu denen das Postfach abgefragt wird.
    poll_times: str = "00:00,12:00,18:00"
    poll_batch_size: int = 50
    #: Abstand der reinen Verbindungstests (Login, Ordner waehlen, keine Mails).
    #: Davon lebt die Statusanzeige: sonst faellt ein abgelehntes Passwort erst
    #: beim naechsten geplanten Abruf auf, also unter Umstaenden Stunden spaeter.
    connection_check_minutes: int = 5

    # --- Mandanten & Sicherheit ---
    #: Fernet-Schluessel fuer Postfach-Passwoerter. Erzeugen: python -m app.admin generate-key
    encryption_key: str = ""
    #: Legt beim ersten Start einen Admin-Nutzer im Standard-Mandanten an,
    #: sofern es noch keinen Nutzer gibt. Danach wirkungslos.
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # --- Putzplan per WhatsApp (Twilio) ---
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    #: Absender, z.B. "whatsapp:+14155238886" (Sandbox) oder die eigene WhatsApp-Nummer.
    twilio_whatsapp_from: str = ""
    #: Freigegebene WhatsApp-Vorlage ("HX...") fuer den Wochenplan. Leer = freier Text,
    #: der nur in der Sandbox bzw. im 24-Stunden-Fenster zugestellt wird.
    twilio_content_sid: str = ""
    #: Eigene Vorlage fuer Aenderungen nach dem Versand. Leer = TWILIO_CONTENT_SID.
    twilio_update_content_sid: str = ""
    #: Vorwahl fuer Nummern in nationaler Schreibweise ("0171 ...").
    phone_default_country_code: str = "49"
    #: Wie WATCH_ENABLED: bei mehreren Instanzen nur auf einer versenden.
    cleaning_dispatch_enabled: bool = True
    # --- Versand von Einmalcodes (Bestaetigung, Passwort-Reset) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    #: Absender, z.B. "Mail Agent <noreply@example.de>". Leer = SMTP_USER.
    smtp_from: str = ""
    #: True: Port 587 mit STARTTLS. False: Port 465 mit SSL von Anfang an.
    smtp_starttls: bool = True

    # --- Registrierung ---
    #: Aus, wenn niemand sich selbst anmelden koennen soll (Nutzer nur per CLI).
    signup_enabled: bool = True

    # --- Weboberflaeche ---
    session_hours: int = 12
    #: Hinter HTTPS auf true setzen.
    cookie_secure: bool = False

    # --- Sonstiges ---
    sample_emails_dir: str = "data/sample_emails"
    exports_dir: str = "data/exports"
    #: Import-Ordner je Mandant liegen darunter: <imports_dir>/tenant-<id>/
    imports_dir: str = "data/imports"
    log_level: str = "INFO"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host)

    @property
    def mail_sender(self) -> str:
        return self.smtp_from or self.smtp_user

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def poll_schedule(self) -> list[time]:
        """Die konfigurierten Abrufzeiten, sortiert und dedupliziert."""
        return parse_poll_times(self.poll_times)


def parse_poll_times(raw: str) -> list[time]:
    """Parst "00:00, 12:00, 18:00" zu Uhrzeiten. Ungueltiges wird verworfen."""
    times: set[time] = set()
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            hour, _, minute = candidate.partition(":")
            times.add(time(int(hour), int(minute or 0)))
        except ValueError:
            logger.warning("Ungueltige Abrufzeit ignoriert: %r", candidate)
    return sorted(times)


@lru_cache
def get_settings() -> Settings:
    return Settings()
