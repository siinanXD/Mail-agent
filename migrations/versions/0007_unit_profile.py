"""Wohnungsprofil: Beschreibung, Hausregeln, Groesse, Reinigungsfenster, Zugang.

Objekte entstehen automatisch aus den Mails - das Profil pflegt der Kunde von
Hand. Alles ist optional, damit bestehende Objekte gueltig bleiben.

Zugangsdaten (Schluessel, Codes, WLAN) liegen verschluesselt in
``access_encrypted`` (Fernet, ``app.crypto``), nie im Klartext.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SPALTEN = (
    ("description", sa.Text()),
    ("house_rules", sa.Text()),
    ("rooms", sa.Integer()),
    ("beds", sa.Integer()),
    ("size_sqm", sa.Integer()),
    ("max_guests", sa.Integer()),
    ("cleaning_window", sa.String(255)),
    ("address", sa.String(255)),
    ("floor", sa.String(64)),
    ("access_encrypted", sa.Text()),
)


def upgrade() -> None:
    for name, typ in SPALTEN:
        op.add_column("units", sa.Column(name, typ, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(SPALTEN):
        op.drop_column("units", name)
