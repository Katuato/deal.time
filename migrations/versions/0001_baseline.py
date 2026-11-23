"""Record the schema created by the previous application version."""

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), unique=True),
        sa.Column("timezone", sa.String()),
        sa.Column("notification", sa.Boolean()),
    )
    op.create_table(
        "consents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), unique=True),
        sa.Column("policy_version", sa.String()),
        sa.Column("accepted_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
    )
    op.create_table(
        "connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id"), unique=True),
        sa.Column("google_token", sa.Text()),
        sa.Column("business_token", sa.Text()),
        sa.Column("status", sa.Boolean()),
    )
    op.create_table(
        "chats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id"), unique=True),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("chat_title", sa.String()),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id")),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("title", sa.String()),
        sa.Column("location", sa.String()),
        sa.Column("starts_at", sa.DateTime()),
        sa.Column("ends_at", sa.DateTime()),
        sa.Column("duration", sa.String()),
        sa.Column("timezone", sa.String()),
        sa.Column("repeating", sa.String()),
        sa.Column("participants", sa.JSON()),
        sa.Column("status", sa.String()),
        sa.Column("confirmed_at", sa.DateTime()),
        sa.Column("google_event_id", sa.String()),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.BigInteger()),
        sa.Column("action", sa.String()),
        sa.Column("data", sa.JSON()),
        sa.Column("status", sa.String()),
        sa.Column("attempts", sa.Integer()),
        sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("next_attempt_step", sa.JSON()),
    )
    for table in ("users", "consents", "connections", "chats", "events", "operations"):
        op.create_index(f"ix_{table}_id", table, ["id"])


def downgrade():
    for table in ("operations", "events", "chats", "connections", "consents", "users"):
        op.drop_table(table)
