"""
Реализация NF-08 через таблицу `operations`.

Формат payload (Operation.data):
    create: {"user_id", "event_data"}
    update: {"user_id", "google_event_id", "new_start_time", "new_end_time", "timezone"}
    delete: {"user_id", "google_event_id"}
Operation.event_id — локальный events.id (необязателен); Google ID лежит в payload
или берётся из events.google_event_id.

Воркер запускается через run_worker() (см. main.py). Строки забираются через
claim_due_operations (FOR UPDATE SKIP LOCKED + lease), так что несколько воркеров
не обработают одну операцию дважды.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select

from database.db import async_session, Operation, Event

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

MAX_ATTEMPTS = 8
LOCK_TIMEOUT = timedelta(minutes=5)      # lease: после него «зависшая» операция берётся снова
POSTPONE_SECONDS = 30                    # отсрочка при ожидании create того же события


def _utcnow() -> datetime:
    """Наивное UTC-время (колонки в БД без часового пояса)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _backoff(attempts: int) -> int:
    return min(30 * (2 ** attempts), 3600)


async def _connector():
    # импорт внутри, чтобы избежать цикла и тянуть Google-библиотеки лениво
    from calendarr import calendar_connector
    return calendar_connector


async def enqueue_operation(
    user_id: int,
    action: str,
    payload: dict[str, Any],
    *,
    event_id: int | None = None,
    delay_seconds: int = 0,
) -> int:
    """action ∈ {'create','update','delete'}; payload см. в docstring модуля."""
    data = {"user_id": user_id, **payload}
    next_at = _utcnow() + timedelta(seconds=delay_seconds)
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


async def claim_due_operations(limit: int = 10) -> list[Operation]:
    """Атомарно забирает готовые операции: статус -> processing, next_attempt_at = lease."""
    now = _utcnow()
    async with async_session() as session:
        rows = (
            await session.execute(
                select(Operation)
                .where(
                    or_(Operation.status == STATUS_PENDING, Operation.status == STATUS_PROCESSING),
                    Operation.next_attempt_at <= now,
                )
                .order_by(Operation.next_attempt_at, Operation.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for op in rows:
            op.status = STATUS_PROCESSING
            op.next_attempt_at = now + LOCK_TIMEOUT
        await session.commit()
        return list(rows)


async def _finish(op_id: int, status: str, *, attempts: int | None = None,
                  retry_in: float | None = None, count_attempt: bool = False) -> None:
    async with async_session() as session:
        op = await session.get(Operation, op_id)
        if not op:
            return
        if count_attempt:
            op.attempts = (op.attempts or 0) + 1
        op.status = status
        if retry_in is not None:
            op.next_attempt_at = _utcnow() + timedelta(seconds=retry_in)
        await session.commit()


async def mark_operation_done(op_id: int) -> None:
    await _finish(op_id, STATUS_DONE)


async def mark_operation_failed(op_id: int) -> None:
    await _finish(op_id, STATUS_FAILED)


async def _retry_later(op_id: int, attempts_before: int) -> bool:
    """Откладывает с backoff, считая попытку. True — ушла в retry, False — исчерпаны попытки."""
    attempts = attempts_before + 1
    if attempts >= MAX_ATTEMPTS:
        logger.error("Operation id=%s исчерпала %s попыток — failed", op_id, MAX_ATTEMPTS)
        await _finish(op_id, STATUS_FAILED, count_attempt=True)
        return False
    await _finish(op_id, STATUS_PENDING, retry_in=_backoff(attempts_before), count_attempt=True)
    return True


async def _postpone(op_id: int) -> None:
    await _finish(op_id, STATUS_PENDING, retry_in=POSTPONE_SECONDS)


async def _create_pending_before(op: Operation) -> bool:
    """Есть ли незавершённый create того же события с меньшим id."""
    if op.event_id is None:
        return False
    async with async_session() as session:
        found = (
            await session.execute(
                select(Operation.id).where(
                    Operation.event_id == op.event_id,
                    Operation.action == "create",
                    Operation.id < op.id,
                    Operation.status.in_([STATUS_PENDING, STATUS_PROCESSING]),
                ).limit(1)
            )
        ).first()
    return found is not None


async def _lookup_google_id(event_id: int) -> str | None:
    async with async_session() as session:
        return (
            await session.execute(select(Event.google_event_id).where(Event.id == event_id))
        ).scalar_one_or_none()


async def _save_google_id(event_id: int, google_id: str) -> None:
    async with async_session() as session:
        ev = await session.get(Event, event_id)
        if ev:
            ev.google_event_id = google_id
            await session.commit()


async def process_pending_calendar_operations(storage, limit: int = 10) -> dict[str, int]:
    """Один проход воркера. Возвращает статистику."""
    connector = await _connector()
    stats = {"processed": 0, "success": 0, "retry": 0, "failed": 0, "postponed": 0}

    for op in await claim_due_operations(limit=limit):
        stats["processed"] += 1
        op_id, action, local_id = op.id, op.action, op.event_id
        attempts = op.attempts or 0
        payload = op.data or {}

        # порядок: update/delete не должны опережать create того же события
        if action in ("update", "delete") and await _create_pending_before(op):
            await _postpone(op_id)
            stats["postponed"] += 1
            continue

        # чтение payload: ошибки структуры не лечатся повтором
        try:
            user_id = payload["user_id"]
            if not user_id:
                raise ValueError("пустой user_id")
            if action == "create":
                event_data = payload["event_data"]
                if not isinstance(event_data, dict):
                    raise TypeError("event_data не dict")
            elif action in ("update", "delete"):
                google_id = payload.get("google_event_id")
                if not google_id and local_id is not None:
                    google_id = await _lookup_google_id(local_id)
                if not google_id:
                    if await _has_pending_create(local_id):
                        await _postpone(op_id)
                        stats["postponed"] += 1
                        continue
                    raise ValueError("нет google_event_id")
                if action == "update":
                    new_start, new_end = payload["new_start_time"], payload["new_end_time"]
                    tz_name = payload.get("timezone", "Europe/Moscow")
            else:
                raise ValueError(f"неизвестный action={action}")
        except (KeyError, ValueError, TypeError) as err:
            logger.error("Operation id=%s: некорректный payload (%s) — failed", op_id, err)
            await mark_operation_failed(op_id)
            stats["failed"] += 1
            continue

        try:
            if action == "create":
                result = await connector.create_event(user_id, storage, event_data)
            elif action == "update":
                result = await connector.update_event_time(
                    user_id, storage, google_id, new_start, new_end, timezone=tz_name
                )
            else:
                result = await connector.delete_event(user_id, storage, google_id)
        except Exception as err:  # noqa: BLE001
            logger.exception("Исключение при обработке operation id=%s: %s", op_id, err)
            ok = await _retry_later(op_id, attempts)
            stats["retry" if ok else "failed"] += 1
            continue

        if result.get("status") == "success":
            if action == "create" and local_id is not None and result.get("event_id"):
                try:
                    await _save_google_id(local_id, result["event_id"])
                except Exception:  # noqa: BLE001
                    logger.exception("Не удалось сохранить google_event_id для events.id=%s", local_id)
            await mark_operation_done(op_id)
            stats["success"] += 1
        elif result.get("need_queue"):
            ok = await _retry_later(op_id, attempts)
            stats["retry" if ok else "failed"] += 1
        else:
            logger.error("Operation id=%s: окончательная ошибка %s", op_id, result.get("error_type"))
            await mark_operation_failed(op_id)
            stats["failed"] += 1

    return stats


async def _has_pending_create(local_id: int | None) -> bool:
    if local_id is None:
        return False
    async with async_session() as session:
        found = (
            await session.execute(
                select(Operation.id).where(
                    Operation.event_id == local_id,
                    Operation.action == "create",
                    Operation.status.in_([STATUS_PENDING, STATUS_PROCESSING]),
                ).limit(1)
            )
        ).first()
    return found is not None


async def create_event_or_enqueue(
    user_id: int,
    storage,
    event_data: dict,
    *,
    local_event_id: int | None = None,
) -> dict:
    """Создаёт событие; при сетевой ошибке (need_queue) ставит операцию в очередь."""
    connector = await _connector()
    result = await connector.create_event(user_id, storage, event_data)
    if result.get("status") == "error" and result.get("need_queue"):
        op_id = await enqueue_operation(
            user_id, "create", {"event_data": event_data},
            event_id=local_event_id, delay_seconds=_backoff(0),
        )
        return {**result, "queued": True, "operation_id": op_id}
    if result.get("status") == "success" and local_event_id is not None and result.get("event_id"):
        try:
            await _save_google_id(local_event_id, result["event_id"])
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось сохранить google_event_id для events.id=%s", local_event_id)
    return result


async def run_worker(storage, interval_seconds: float = 30.0) -> None:
    """Бесконечный цикл воркера; завершается по CancelledError."""
    logger.info("Воркер очереди календаря запущен (интервал %ss)", interval_seconds)
    try:
        while True:
            try:
                stats = await process_pending_calendar_operations(storage)
                if stats["processed"]:
                    logger.info("Очередь календаря: %s", stats)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка итерации воркера очереди календаря")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("Воркер очереди календаря остановлен")
