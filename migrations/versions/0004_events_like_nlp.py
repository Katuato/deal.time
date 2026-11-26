"""events: структура как у to_event_record NLP-модуля

* duration: VARCHAR с минутами -> INTERVAL (NLP отдаёт timedelta);
* status: до подтверждения 'draft' (как в NLP) вместо 'waiting', NOT NULL, допустимые значения
  draft / queued / confirmed / failed / declined;
* participants: JSON -> JSONB, NOT NULL, по умолчанию [] (NLP отдаёт список [{"name": ...}]);
* google_event_id: индекс для поиска события по id в Google.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_events_like_nlp"
down_revision = "0003_events_rrule"
branch_labels = None
depends_on = None

STATUSES = "('draft', 'queued', 'confirmed', 'failed', 'declined')"


def upgrade():
    # длительность: '90' (минуты строкой) -> interval '90 minutes'; нечисловые значения обнуляются
    op.alter_column(
        "events", "duration", type_=sa.Interval(), existing_type=sa.String(),
        postgresql_using="CASE WHEN duration ~ '^[0-9]+$' THEN (duration || ' minutes')::interval END",
    )

    op.execute("UPDATE events SET status = 'draft' WHERE status IS NULL OR status = 'waiting'")
    op.execute(f"UPDATE events SET status = 'declined' WHERE status NOT IN {STATUSES}")
    op.alter_column("events", "status", existing_type=sa.String(), nullable=False, server_default="draft")
    op.create_check_constraint("ck_events_status", "events", f"status IN {STATUSES}")

    op.execute("UPDATE events SET participants = '[]' WHERE participants IS NULL")
    op.alter_column(
        "events", "participants", type_=postgresql.JSONB(), existing_type=sa.JSON(),
        postgresql_using="participants::jsonb", nullable=False, server_default=sa.text("'[]'::jsonb"),
    )

    op.create_index("ix_events_google_event_id", "events", ["google_event_id"])


def downgrade():
    op.drop_index("ix_events_google_event_id", table_name="events")
    op.alter_column("events", "participants", type_=sa.JSON(), existing_type=postgresql.JSONB(),
                    postgresql_using="participants::json", nullable=True, server_default=None)
    op.drop_constraint("ck_events_status", "events", type_="check")
    op.alter_column("events", "status", existing_type=sa.String(), nullable=True, server_default=None)
    op.execute("UPDATE events SET status = 'waiting' WHERE status = 'draft'")
    op.alter_column("events", "duration", type_=sa.String(), existing_type=sa.Interval(),
                    postgresql_using="(EXTRACT(EPOCH FROM duration) / 60)::int::text")
