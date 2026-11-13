"""
Реализация NF-08 через таблицу `operations`.

⚠️ Схема `operations` сейчас несовершенна:
    * нет колонки user_id — кладём внутрь `data["user_id"]`;
    * `event_id` (BigInteger) не подходит для Google event ID (строка) —
      кладём Google ID в `data["google_event_id"]`, колонку оставляем NULL;
    * воркер (process_pending) — скелет; место запуска (asyncio task / cron /
      отдельный процесс) должен определить архитектор.
"""
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.db import async_session, Operation

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


async def enqueue_operation(
    user_id: int,
    action: str,
    payload: dict[str, Any],
    *,
    event_id: int | None = None,
    delay_seconds: int = 0,
) -> int:
    """action ∈ {'create','update','delete'}; payload полностью описывает операцию."""
    data = {"user_id": user_id, **payload}
    next_at = datetime.utcnow() + timedelta(seconds=delay_seconds)
    async with async_session() as session:
        op = Operation(
            event_id=event_id,
            action=action,
            data=data,
            status=STATUS_PENDING,
            attempts=0,
            next_attempt_at=next_at,
        )
        session.add(op)
        await session.commit()
        await session.refresh(op)
        logger.info("В очередь поставлена операция id=%s action=%s", op.id, action)
        return op.id


async def fetch_due_operations(limit: int = 10) -> list[Operation]:
    now = datetime.utcnow()
    async with async_session() as session:
        rows = (
            await session.execute(
                select(Operation)
                .where(Operation.status == STATUS_PENDING, Operation.next_attempt_at <= now)
                .order_by(Operation.next_attempt_at)
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)


async def mark_operation_done(op_id: int) -> None:
    async with async_session() as session:
        op = await session.get(Operation, op_id)
        if op:
            op.status = STATUS_DONE
            await session.commit()


async def mark_operation_failed(
    op_id: int, retry_delay_seconds: int | None = None
) -> None:
    """retry_delay=None ⇒ окончательный провал (неретраибельная ошибка)."""
    async with async_session() as session:
        op = await session.get(Operation, op_id)
        if not op:
            return
        op.attempts = (op.attempts or 0) + 1
        if retry_delay_seconds is None:
            op.status = STATUS_FAILED
        else:
            op.next_attempt_at = datetime.utcnow() + timedelta(seconds=retry_delay_seconds)
        await session.commit()


async def process_pending_calendar_operations(storage, limit: int = 10) -> dict[str, int]:
    """
    Скелет воркера. Вызывать периодически (asyncio.create_task в lifespan
    бота / cron / отдельный процесс — решает Анисимов).
    """
    # импорт внутри, чтобы избежать цикла
    from .calendar_connector import create_event, update_event_time, delete_event

    stats = {"processed": 0, "success": 0, "retry": 0, "failed": 0}
    for op in await fetch_due_operations(limit=limit):
        stats["processed"] += 1
        payload = op.data or {}
        user_id = payload.get("user_id")
        if not user_id:
            logger.error("Operation id=%s без user_id — окончательный провал", op.id)
            await mark_operation_failed(op.id, retry_delay_seconds=None)
            stats["failed"] += 1
            continue

        try:
            if op.action == "create":
                result = await create_event(user_id, storage, payload["event_data"])
            elif op.action == "update":
                u = payload["update"]
                result = await update_event_time(
                    user_id, storage,
                    event_id=u["event_id"],
                    new_start_time=u["new_start_time"],
                    new_end_time=u["new_end_time"],
                    timezone=u.get("timezone", "Europe/Moscow"),
                )
            elif op.action == "delete":
                result = await delete_event(user_id, storage, payload["google_event_id"])
            else:
                logger.error("Неизвестный action=%s у operation id=%s", op.action, op.id)
                await mark_operation_failed(op.id, retry_delay_seconds=None)
                stats["failed"] += 1
                continue
        except Exception as err:  # noqa: BLE001
            logger.exception("Исключение при обработке operation id=%s: %s", op.id, err)
            await mark_operation_failed(op.id, retry_delay_seconds=60)
            stats["retry"] += 1
            continue

        if result.get("status") == "success":
            await mark_operation_done(op.id)
            stats["success"] += 1
        elif result.get("need_queue"):
            backoff = min(30 * (2 ** (op.attempts or 0)), 3600)
            await mark_operation_failed(op.id, retry_delay_seconds=backoff)
            stats["retry"] += 1
        else:
            await mark_operation_failed(op.id, retry_delay_seconds=None)
            stats["failed"] += 1

    return stats