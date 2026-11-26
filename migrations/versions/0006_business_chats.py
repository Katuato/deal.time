"""Allow a business account to have more than one observed chat."""

from alembic import op

revision = "0006_business_chats"
down_revision = "0005_events_status_deleted"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_chats_user_chat", "chats", ["user_id", "chat_id"])
    op.drop_constraint("chats_user_id_key", "chats", type_="unique")


def downgrade():
    # PostgreSQL rejects the old constraint if it would conflict with existing rows.
    op.create_unique_constraint("chats_user_id_key", "chats", ["user_id"])
    op.drop_constraint("uq_chats_user_chat", "chats", type_="unique")
