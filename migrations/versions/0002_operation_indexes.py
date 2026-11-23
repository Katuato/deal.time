"""Index the queue worker's due and dependent operation queries."""

import sqlalchemy as sa
from alembic import op

revision = "0002_operation_indexes"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_operations_due",
        "operations",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )
    op.create_index("ix_operations_event_action_status", "operations", ["event_id", "action", "status"])


def downgrade():
    op.drop_index("ix_operations_event_action_status", table_name="operations")
    op.drop_index("ix_operations_due", table_name="operations")
