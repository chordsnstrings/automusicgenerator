"""publications

Revision ID: d7c3915ab204
Revises: b4e18a2c9f31
Created: 2026-08-27 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd7c3915ab204'
down_revision = 'b4e18a2c9f31'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per clip per platform, and the unique constraint says so. Without
    # it a re-run of `dailyfive publish` after a timeout would upload the same
    # song twice and leave two rows claiming to be the same video's metrics —
    # which is worse than a failed publish, because both rows look real.
    #
    # Indexed on status, unlike the genre columns next door. This table has a
    # reader that genuinely filters: the metrics job asks for every publication
    # that is live, every day, forever, and that set stays small while the table
    # grows a row per platform per song.
    op.create_table(
        "publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("clip_id", sa.Integer(),
                  sa.ForeignKey("clips.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("external_id", sa.String(length=120)),
        sa.Column("url", sa.String(length=400)),
        sa.Column("error", sa.Text()),
        sa.Column("views", sa.Integer()),
        sa.Column("likes", sa.Integer()),
        sa.Column("comments", sa.Integer()),
        sa.Column("shares", sa.Integer()),
        sa.Column("metrics", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("metrics_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("clip_id", "platform", name="uq_publication"),
    )
    op.create_index("ix_publications_clip_id", "publications", ["clip_id"])
    op.create_index("ix_publications_platform", "publications", ["platform"])
    op.create_index("ix_publications_status", "publications", ["status"])
    op.create_index("ix_publications_external_id", "publications", ["external_id"])


    # The one table in this schema that holds a live secret. It exists at all
    # because a TikTok refresh token is single-use — the platform issues a new
    # one on every refresh — so a credential in an env var works exactly once.
    # backup.py excludes its contents for the obvious reason.
    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("access_token", sa.Text()),
        sa.Column("refresh_token", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("account_id", sa.String(length=120)),
        sa.Column("scope", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("platform", name="uq_oauth_platform"),
    )
    op.create_index("ix_oauth_tokens_platform", "oauth_tokens", ["platform"])


def downgrade() -> None:
    op.drop_index("ix_oauth_tokens_platform", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
    op.drop_index("ix_publications_external_id", table_name="publications")
    op.drop_index("ix_publications_status", table_name="publications")
    op.drop_index("ix_publications_platform", table_name="publications")
    op.drop_index("ix_publications_clip_id", table_name="publications")
    op.drop_table("publications")
