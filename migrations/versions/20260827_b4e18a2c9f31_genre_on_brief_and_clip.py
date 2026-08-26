"""genre on brief and clip

Revision ID: b4e18a2c9f31
Revises: c1f7a2d40e83
Created: 2026-08-27 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b4e18a2c9f31'
down_revision = 'c1f7a2d40e83'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No index. aggregate() and genres.scores() both select the rows and bucket
    # in Python, so nothing would consult one; the table grows fourteen rows a
    # day, about 5,100 a year. An index no reader uses is a write cost with no
    # reader.
    #
    # No server_default, and existing rows keep NULL deliberately. The live
    # database holds six briefs and twelve clips from 2026-08-26 and there is
    # no way to derive their genre without a person or a model reading the
    # style strings back — and a model inferring a genre after the fact is
    # exactly the fabricated track record the codex and the Director's prompt
    # forbid. NULL is queryable and true: WHERE genre_family IS NULL counts
    # "songs made before the studio recorded genre". A server_default of 'pop'
    # across twelve rows would invent a track record on day one of the feature
    # that exists to stop inventing them.
    for table in ("briefs", "clips"):
        op.add_column(table, sa.Column("genre_family", sa.String(length=24), nullable=True))
        op.add_column(table, sa.Column("genre", sa.String(length=40), nullable=True))


def downgrade() -> None:
    for table in ("briefs", "clips"):
        op.drop_column(table, "genre")
        op.drop_column(table, "genre_family")
