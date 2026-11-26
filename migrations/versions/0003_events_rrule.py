"""events.repeating -> events.rrule

Колонка repeating задумывалась под правило повторения (RFC 5545). Переименовываем её в rrule
(как в NLP-модуле и calendarr). Если колонки repeating нет — просто добавляем rrule.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_events_rrule"
down_revision = "0002_operation_indexes"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("events")}


def upgrade():
    columns = _columns()
    if "rrule" in columns:
        return
    if "repeating" in columns:
        op.alter_column("events", "repeating", new_column_name="rrule")
    else:
        op.add_column("events", sa.Column("rrule", sa.String()))


def downgrade():
    op.alter_column("events", "rrule", new_column_name="repeating")
