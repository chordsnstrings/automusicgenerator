"""stored files external storage

Revision ID: c1f7a2d40e83
Revises: 6ad48bf109cf
Created: 2026-08-26 10:04:11.000000
"""
from __future__ import annotations

from alembic import op


revision = 'c1f7a2d40e83'
down_revision = '6ad48bf109cf'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # substr() over a TOASTed bytea is only a partial detoast when the value is
    # stored out-of-line *uncompressed*. Audio is incompressible, so today's
    # rows land that way by luck; EXTERNAL makes it a property of the schema
    # rather than of the payload, which is what the streaming read path in
    # web/app.py relies on to stay bounded.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE stored_files ALTER COLUMN data SET STORAGE EXTERNAL")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE stored_files ALTER COLUMN data SET STORAGE EXTENDED")
