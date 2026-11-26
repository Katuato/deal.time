"""Persist Mini App tasks and read state, and index the event history."""

import sqlalchemy as sa
from alembic import op

revision = "0007_miniapp"
down_revision = "0006_business_chats"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("google_html_link", sa.String()))
    op.create_index("ix_events_user_id_id", "events", ["user_id", "id"])
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("time", sa.Time(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"])
    op.create_table(
        "event_notice_reads",
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
    )


def downgrade():
    op.drop_table("event_notice_reads")
    op.drop_index("ix_tasks_user_id", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_events_user_id_id", table_name="events")
    op.drop_column("events", "google_html_link")
