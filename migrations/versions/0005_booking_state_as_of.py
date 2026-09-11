"""Buchungen merken sich, wie aktuell ihr Zeitraum ist.

Wurde eine Umbuchung vor der urspruenglichen Buchungsmail importiert (oder die
Buchungsmail spaeter erneut), setzte die aeltere Mail Zeitraum und Objekt
zurueck. ``state_as_of`` haelt den Eingang der neuesten Mail fest, die den
Stand bestimmt hat.

Ohne Backfill: Fuer bestehende Buchungen leitet die Anwendung den Wert aus
Quellmail und Aenderungen ab, solange die Spalte leer ist. Ein UPDATE hier
wuerde bei einem Owner ohne BYPASSRLS an FORCE ROW LEVEL SECURITY scheitern.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("state_as_of", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "state_as_of")
