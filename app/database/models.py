"""SQLAlchemy-Modelle. PostgreSQL ist die Source of Truth."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.config import get_settings

EMAIL_TYPES = (
    "booking",
    "cancellation",
    "change",
    "request",
    "complaint",
    "other",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def _tenant_fk() -> Mapped[int]:
    """Jede Datentabelle haengt an genau einem Mandanten."""
    return mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )


class Tenant(Base):
    """Mandant - eine Vermietung mit eigenem Postfach und eigenen Daten."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class User(Base):
    """Anmeldung. Ein Nutzer gehoert zu genau einem Mandanten."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    tenant: Mapped[Tenant] = relationship()


class Mailbox(Base):
    """IMAP-Postfach eines Mandanten.

    Das Passwort liegt verschluesselt (``app.crypto``), nicht im Klartext.
    """

    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=993)
    username: Mapped[str] = mapped_column(String(255))
    password_encrypted: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(String(128), default="INBOX")
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    since_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: IMAP-Cursor: hoechste bereits verarbeitete UID. Gilt nur zusammen mit der
    #: UIDVALIDITY, unter der sie vergeben wurde - aendert der Server die, faengt
    #: der Abruf von vorn an (die Dublettenpruefung verhindert Doppelimporte).
    last_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uid_validity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Erste fehlgeschlagene UID, vor der der Cursor wartet, und ihre Versuche.
    retry_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    tenant: Mapped[Tenant] = relationship()


class Email(Base):
    __tablename__ = "emails"
    __table_args__ = (UniqueConstraint("tenant_id", "provider_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    provider_message_id: Mapped[str] = mapped_column(String(255))
    sender: Mapped[str] = mapped_column(String(255))
    recipient: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    email_type: Mapped[str] = mapped_column(String(32), default="other", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    bookings: Mapped[list["Booking"]] = relationship(back_populates="source_email")


class Unit(Base):
    """Ferienwohnung / Wohnung / Haus.

    ``normalized_name`` ist der Schluessel fuer die Dublettenvermeidung:
    "FeWo Seeblick", "Ferienwohnung Seeblick" und "seeblick" landen auf
    demselben Objekt.
    """

    __tablename__ = "units"
    __table_args__ = (UniqueConstraint("tenant_id", "normalized_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    bookings: Mapped[list["Booking"]] = relationship(back_populates="unit")


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (UniqueConstraint("tenant_id", "booking_reference"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    booking_reference: Mapped[str] = mapped_column(String(64))
    guest_name: Mapped[str] = mapped_column(String(255), index=True)
    arrival_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    departure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="confirmed")
    unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("units.id", ondelete="SET NULL"), nullable=True
    )
    source_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL"), nullable=True
    )
    #: Eingang der neuesten Mail, die die Buchung als Ganzes beschrieben hat
    #: (Buchungs-, Storno- oder anlegende Umbuchungsmail). Umbuchungen einzelner
    #: Angaben stehen mit Zeitpunkt in booking_changes - aus beidem ergibt sich je
    #: Feld, ob eine spaeter importierte aeltere Mail es noch setzen darf.
    state_as_of: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    unit: Mapped[Unit | None] = relationship(back_populates="bookings")
    source_email: Mapped[Email | None] = relationship(back_populates="bookings")


class BookingChange(Base):
    """Protokolliert Umbuchungen (geaenderte An-/Abreise oder Objektwechsel)."""

    __tablename__ = "booking_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE")
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime)
    field: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL"), nullable=True
    )

    booking: Mapped[Booking] = relationship()


class Cancellation(Base):
    __tablename__ = "cancellations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=True
    )
    cancelled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL"), nullable=True
    )

    booking: Mapped[Booking | None] = relationship()


class EmailEmbedding(Base):
    __tablename__ = "email_embeddings"
    __table_args__ = (UniqueConstraint("email_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    chunk: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(get_settings().embedding_dim))
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class StaffMember(Base):
    """Reinigungskraft. Bekommt den Putzplan fuer ihre Wohnungen per WhatsApp."""

    __tablename__ = "staff_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    name: Mapped[str] = mapped_column(String(255))
    #: E.164, z.B. "+491711234567" - so adressiert WhatsApp (``app.staff.phone``).
    phone: Mapped[str] = mapped_column(String(32))
    #: Inaktive behalten ihre Wohnungen, bekommen aber keine Nachrichten.
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    assignments: Mapped[list["StaffUnit"]] = relationship(
        cascade="all, delete-orphan", order_by="StaffUnit.id"
    )


class StaffUnit(Base):
    """Welche Wohnung ein Mitarbeiter putzt. Eine Wohnung kann mehrere haben."""

    __tablename__ = "staff_units"
    __table_args__ = (UniqueConstraint("staff_id", "unit_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff_members.id", ondelete="CASCADE"), index=True
    )
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), index=True)

    unit: Mapped[Unit] = relationship()


class CleaningSchedule(Base):
    """Wann der Putzplan automatisch rausgeht - hoechstens eine Zeile je Mandant."""

    __tablename__ = "cleaning_schedules"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 0 = Montag ... 6 = Sonntag
    send_weekday: Mapped[int] = mapped_column(Integer, default=6)
    send_time: Mapped[time] = mapped_column(Time, default=time(18, 0))
    #: Seit wann Tag und Uhrzeit so gelten. Termine davor holt der Versand nicht
    #: nach - sonst ginge beim Einschalten mitten in der Woche sofort ein Plan raus.
    active_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CleaningDispatch(Base):
    """Eine Putzplan-Nachricht an einen Mitarbeiter - zugestellt oder gescheitert.

    ``tasks`` haelt die Reinigungen der Woche fest, die der Mitarbeiter mit dieser
    Nachricht kannte. Gegen den letzten zugestellten Stand wird nach jedem
    Mail-Abruf verglichen, ob er eine Aenderung bekommen muss.
    """

    __tablename__ = "cleaning_dispatches"
    __table_args__ = (Index("ix_cleaning_dispatches_staff_week", "staff_id", "week_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff_members.id", ondelete="CASCADE"))
    #: Montag der Woche, um die es geht
    week_start: Mapped[date] = mapped_column(Date)
    #: "plan" (Wochenplan) oder "update" (Aenderung nach dem Versand)
    kind: Mapped[str] = mapped_column(String(16))
    success: Mapped[bool] = mapped_column(Boolean)
    tasks: Mapped[list] = mapped_column(JSON, default=list)
    provider_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Lokale Serverzeit, wie der Versandtermin
    created_at: Mapped[datetime] = mapped_column(DateTime)


#: Tabellen, fuer die Row-Level-Security greift (Mandantentrennung).
TENANT_TABLES = (
    "emails",
    "units",
    "bookings",
    "cancellations",
    "booking_changes",
    "email_embeddings",
    "staff_members",
    "staff_units",
    "cleaning_schedules",
    "cleaning_dispatches",
)

#: Tabellen ohne pgvector-Spalte - nutzbar auch auf SQLite (Tests).
STRUCTURED_TABLES = [
    Tenant.__table__,
    User.__table__,
    Mailbox.__table__,
    Email.__table__,
    Unit.__table__,
    Booking.__table__,
    Cancellation.__table__,
    BookingChange.__table__,
    StaffMember.__table__,
    StaffUnit.__table__,
    CleaningSchedule.__table__,
    CleaningDispatch.__table__,
]
