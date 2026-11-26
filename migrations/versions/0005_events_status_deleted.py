"""events.status: добавлен статус deleted

deleted — событие удалено из Google Календаря кнопкой «Удалить» (или «Отменить серию»
для повторяющегося события) в карточке бота.
"""

from alembic import op

revision = "0005_events_status_deleted"
down_revision = "0004_events_like_nlp"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_events_status", "events", type_="check")
    op.create_check_constraint(
        "ck_events_status", "events",
        "status IN ('draft', 'queued', 'confirmed', 'failed', 'declined', 'deleted')",
    )


def downgrade():
    # удалённые события при откате считаем отменёнными пользователем
    op.execute("UPDATE events SET status = 'declined' WHERE status = 'deleted'")
    op.drop_constraint("ck_events_status", "events", type_="check")
    op.create_check_constraint(
        "ck_events_status", "events", "status IN ('draft', 'queued', 'confirmed', 'failed', 'declined')"
    )
