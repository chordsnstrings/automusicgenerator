"""language on brief and clip

Revision ID: a2f6b81c4d59
Revises: d7c3915ab204
Created: 2026-08-31 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a2f6b81c4d59'
down_revision = 'd7c3915ab204'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL means English throughout, and that is a fact rather than a gap — most
    # songs are and always will be. So no server_default and no backfill: the
    # forty-odd briefs written before this feature existed are correctly null,
    # and "WHERE language IS NULL" counts English-only songs rather than
    # "songs from before we recorded it", which is what the genre columns had to
    # live with.
    #
    # No index, same arithmetic as the genre columns: the aggregates select the
    # rows and bucket in Python, the table grows fourteen rows a day, and an
    # index nothing reads is a write cost with no reader.
    op.add_column("briefs", sa.Column("language", sa.String(length=8), nullable=True))
    op.add_column("briefs", sa.Column("language_placement",
                                      sa.String(length=24), nullable=True))
    op.add_column("clips", sa.Column("language", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("clips", "language")
    op.drop_column("briefs", "language_placement")
    op.drop_column("briefs", "language")
