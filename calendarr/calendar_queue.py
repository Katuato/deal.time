"""
Реализация NF-08 через таблицу `operations`.

Формат payload (Operation.data):
    create: {"user_id", "event_data", "google_event_id" (для подтверждённых событий)}
    update: {"user_id", "google_event_id", "new_start_time", "new_end_time", "timezone"}
    delete: {"user_id", "google_event_id"}
Operation.event_id — локальный events.id (необязателен); Google ID лежит в payload
или берётся из events.google_event_id.

Воркер запускается через run_worker() (см. main.py). Строки забираются через
claim_due_operations (FOR UPDATE SKIP LOCKED + lease). После истечения lease
повтор использует сохранённый Google ID; завершить операцию может текущий воркер.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

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


def stage_operation(
    session, user_id: int, action: str, payload: dict[str, Any],
    event_id: int | None, delay_seconds: int = 0,
) -> Operation:
    """Add a calendar operation to the caller's database transaction."""
    operation = Operation(
        event_id=event_id,
        action=action,
        data={**payload, "user_id": user_id},
        status=STATUS_PENDING,
        attempts=0,
        next_attempt_at=_utcnow() + timedelta(seconds=delay_seconds),
    )
    session.add(operation)
    return operation


def stage_create(session, event: Event, event_data: dict[str, Any], **metadata) -> Operation:
    event.status = "queued"
    event.confirmed_at = _utcnow()
    payload = {**metadata, "event_data": event_data, "google_event_id": uuid4().hex}
    return stage_operation(session, event.user_id, "create", payload, event.id)


async def enqueue_operation(
    user_id: int,
    action: str,
    payload: dict[str, Any],
    *,
    event_id: int | None = None,
    delay_seconds: int = 0,
) -> int:
    """action ∈ {'create','update','delete'}; payload см. в docstring модуля."""
    async with async_session.begin() as session:
        op = stage_operation(session, user_id, action, payload, event_id, delay_seconds)
        await session.flush()
        op_id = op.id
    logger.info("В очередь поставлена операция id=%s", op_id)
    return op_id


async def confirm_event_and_enqueue(
    user_id: int,
    event_id: int,
    build_event_data: Callable[[Event], dict[str, Any]],
) -> int | None:
    """Подтверждает событие владельца и сохраняет операцию до обращения к Google."""
    async with async_session.begin() as session:
        event = await session.scalar(
            select(Event)
            .where(Event.id == event_id, Event.user_id == user_id, Event.status == "draft")
            .with_for_update()
        )
        if event is None:
            return None

        operation = stage_create(session, event, build_event_data(event))
        await session.flush()
        op_id = operation.id
    logger.info("Подтверждение события сохранено, операция id=%s", op_id)
    return op_id


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


async def _finish(op_id: int, status: str, *, lease_until: datetime | None = None,
                  retry_in: float | None = None, count_attempt: bool = False,
                  google_id: str | None = None, html_link: str | None = None) -> bool:
    async with async_session.begin() as session:
        op = await session.get(Operation, op_id, with_for_update=True)
        if op is None:
            return False
        if lease_until is not None and (
            op.status != STATUS_PROCESSING or op.next_attempt_at != lease_until
        ):
            return False
        if count_attempt:
            op.attempts = (op.attempts or 0) + 1
        op.status = status
        if retry_in is not None:
            op.next_attempt_at = _utcnow() + timedelta(seconds=retry_in)

        payload = op.data if isinstance(op.data, dict) else {}
        restore_deleted = op.action == "delete" and payload.get("miniapp") and status == STATUS_FAILED
        finish_create = op.action == "create" and status in (STATUS_DONE, STATUS_FAILED)
        if op.event_id is not None and (finish_create or restore_deleted):
            user_id = payload.get("user_id")
            if type(user_id) is not int:
                return True
            event = await session.scalar(
                select(Event).where(Event.id == op.event_id, Event.user_id == user_id)
                .with_for_update()
            )
            if event is not None:
                if restore_deleted and event.status == "deleted":
                    event.status = payload.get("previous_status", "confirmed")
                if finish_create and status == STATUS_DONE and google_id:
                    event.google_event_id = google_id
                    if html_link:
                        event.google_html_link = html_link
                if finish_create and payload.get("google_event_id") and event.status == "queued":
                    event.status = "confirmed" if status == STATUS_DONE else "failed"
    return True


async def mark_operation_done(op_id: int) -> None:
    await _finish(op_id, STATUS_DONE)


async def mark_operation_failed(op_id: int) -> None:
    await _finish(op_id, STATUS_FAILED)


async def _retry_later(op: Operation) -> str | None:
    """Откладывает с backoff или завершает операцию после последней попытки."""
    attempts_before = op.attempts or 0
    attempts = attempts_before + 1
    if attempts >= MAX_ATTEMPTS:
        if await _finish(op.id, STATUS_FAILED, lease_until=op.next_attempt_at, count_attempt=True):
            logger.error("Operation id=%s исчерпала %s попыток", op.id, MAX_ATTEMPTS)
            return "failed"
        return None
    if await _finish(op.id, STATUS_PENDING, lease_until=op.next_attempt_at,
                     retry_in=_backoff(attempts_before), count_attempt=True):
        return "retry"
    return None


async def _postpone(op: Operation) -> bool:
    return await _finish(op.id, STATUS_PENDING, lease_until=op.next_attempt_at,
                         retry_in=POSTPONE_SECONDS)


async def _operation_pending_before(op: Operation) -> bool:
    """Keep Mini App writes in order, including retries of an older operation."""
    if op.event_id is None:
        return False
    statement = select(Operation.id).where(
        Operation.event_id == op.event_id, Operation.id < op.id,
        Operation.status.in_([STATUS_PENDING, STATUS_PROCESSING]),
    )
    if not (op.data or {}).get("miniapp"):
        statement = statement.where(Operation.action == "create")
    async with async_session() as session:
        return await session.scalar(statement.limit(1)) is not None


async def _lookup_google_id(user_id: int, event_id: int) -> str | None:
    async with async_session() as session:
        return (
            await session.execute(
                select(Event.google_event_id).where(Event.id == event_id, Event.user_id == user_id)
            )
        ).scalar_one_or_none()


async def _save_google_id(user_id: int, event_id: int, google_id: str) -> None:
    async with async_session() as session:
        ev = await session.scalar(select(Event).where(Event.id == event_id, Event.user_id == user_id))
        if ev:
            ev.google_event_id = google_id
            await session.commit()


# Колбэк об окончательном результате операции: on_result(op, outcome, result), где
#   op      — {"id", "action", "event_id", "user_id", "data"}
#   outcome — "done" или "failed" (повторов больше не будет)
#   result  — ответ calendar_connector ({"status", "error_type", "message", ...})
OnResult = Callable[[dict, str, dict], Awaitable[None]]


async def _report(on_result: Optional[OnResult], op: Operation, outcome: str, result: dict) -> None:
    """Сообщает об окончательном результате; ошибка колбэка не ломает воркер."""
    if on_result is None:
        return
    payload = op.data if isinstance(op.data, dict) else {}
    info = {"id": op.id, "action": op.action, "event_id": op.event_id,
            "user_id": payload.get("user_id"), "data": payload}
    try:
        await on_result(info, outcome, result)
    except Exception as err:  # noqa: BLE001
        logger.error("Ошибка колбэка on_result для operation id=%s (%s)", op.id, type(err).__name__)


async def process_pending_calendar_operations(storage, limit: int = 10,
                                              on_result: Optional[OnResult] = None) -> dict[str, int]:
    """Один проход воркера. Возвращает статистику."""
    connector = await _connector()
    stats = {"processed": 0, "success": 0, "retry": 0, "failed": 0, "postponed": 0}

    for op in await claim_due_operations(limit=limit):
        stats["processed"] += 1
        op_id, action, local_id = op.id, op.action, op.event_id
        payload = op.data or {}

        # порядок: update/delete не должны опережать create того же события
        if action in ("update", "delete") and await _operation_pending_before(op):
            stats["postponed"] += await _postpone(op)
            continue

        # чтение payload: ошибки структуры не лечатся повтором
        try:
            user_id = payload["user_id"]
            if type(user_id) is not int or not user_id:
                raise ValueError("некорректный user_id")
            if action == "create":
                event_data = payload["event_data"]
                if not isinstance(event_data, dict):
                    raise TypeError("event_data не dict")
                if payload.get("google_event_id"):
                    event_data = {**event_data, "google_event_id": payload["google_event_id"]}
            elif action in ("update", "delete"):
                google_id = payload.get("google_event_id")
                if not google_id and local_id is not None:
                    google_id = await _lookup_google_id(user_id, local_id)
                if not google_id:
                    if await _has_pending_create(local_id):
                        stats["postponed"] += await _postpone(op)
                        continue
                    raise ValueError("нет google_event_id")
                if action == "update":
                    new_start, new_end = payload["new_start_time"], payload["new_end_time"]
                    tz_name = payload.get("timezone", "Europe/Moscow")
            else:
                raise ValueError(f"неизвестный action={action}")
        except (KeyError, ValueError, TypeError) as err:
            logger.error("Operation id=%s: некорректный payload", op_id)
            if await _finish(op_id, STATUS_FAILED, lease_until=op.next_attempt_at):
                stats["failed"] += 1
                await _report(on_result, op, "failed", {"status": "error", "error_type": "bad_payload",
                                                         "message": str(err)})
            continue

        try:
            if action == "create":
                result = await connector.create_event(user_id, storage, event_data)
            elif action == "update":
                changes = {key: payload[key] for key in ("summary", "recurrence") if key in payload}
                result = await connector.update_event_time(
                    user_id, storage, google_id, new_start, new_end, timezone=tz_name, **changes,
                )
            else:
                result = await connector.delete_event(user_id, storage, google_id)
        except Exception as err:  # noqa: BLE001
            logger.error("Ошибка обработки operation id=%s (%s)", op_id, type(err).__name__)
            outcome = await _retry_later(op)
            if outcome:
                stats[outcome] += 1
            if outcome == "failed":
                await _report(on_result, op, "failed", {"status": "error", "error_type": "exception",
                                                         "message": type(err).__name__})
            continue

        if result.get("status") == "success":
            google_id = result.get("event_id") or payload.get("google_event_id")
            if await _finish(op_id, STATUS_DONE, lease_until=op.next_attempt_at, google_id=google_id,
                             html_link=result.get("html_link")):
                stats["success"] += 1
                await _report(on_result, op, "done", result)
        elif result.get("need_queue"):
            outcome = await _retry_later(op)
            if outcome:
                stats[outcome] += 1
            if outcome == "failed":
                await _report(on_result, op, "failed", result)
        else:
            logger.error("Operation id=%s: окончательная ошибка Google Calendar", op_id)
            if await _finish(op_id, STATUS_FAILED, lease_until=op.next_attempt_at):
                stats["failed"] += 1
                await _report(on_result, op, "failed", result)

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
            await _save_google_id(user_id, local_event_id, result["event_id"])
        except Exception as err:  # noqa: BLE001
            logger.error("Не удалось сохранить Google ID events.id=%s (%s)", local_event_id, type(err).__name__)
    return result


_wakeup = asyncio.Event()


def wake_worker() -> None:
    """Будит воркер раньше интервала — например, сразу после постановки события в очередь."""
    _wakeup.set()


async def run_worker(storage, interval_seconds: float = 30.0,
                     on_result: Optional[OnResult] = None) -> None:
    """Бесконечный цикл воркера; завершается по CancelledError.
    on_result — колбэк об окончательном результате операции (например, уведомление пользователю)."""
    logger.info("Воркер очереди календаря запущен (интервал %ss)", interval_seconds)
    try:
        while True:
            try:
                stats = await process_pending_calendar_operations(storage, on_result=on_result)
                if stats["processed"]:
                    logger.info("Очередь календаря: %s", stats)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                logger.error("Ошибка итерации воркера очереди календаря (%s)", type(err).__name__)
            try:
                await asyncio.wait_for(_wakeup.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
            _wakeup.clear()
    except asyncio.CancelledError:
        logger.info("Воркер очереди календаря остановлен")
