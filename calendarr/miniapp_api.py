"""Same-origin Mini App endpoints. Every database query is scoped to the signed Telegram user."""

import asyncio
import logging
import re
from datetime import date as Date
from datetime import datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from starlette.exceptions import HTTPException as StarletteHTTPException

from calendarr import oauth
from calendarr.calendar_connector import check_connection
from calendarr.calendar_queue import stage_create, stage_operation, wake_worker
from calendarr.miniapp_auth import verified_user_id
from calendarr.nlp_adapter import build_event_data
from calendarr.storage import DBTokenStorage
from config import bot
from database.db import Chat, Connection, Consent, Event, EventNoticeRead, Operation, Task, User, async_session
from modules.speech_runtime import FORMATS, MAX_AUDIO_BYTES, SpeechBusy, transcribe_miniapp
from modules.nlp_module import DEFAULT_TZ, MAX_TEXT_LEN, DialogueState, process_dialogue_message, to_event_record

logger = logging.getLogger(__name__)
api = FastAPI(openapi_url=None, redirect_slashes=False)
_parse_lock = asyncio.Lock()
ID_MAX = 2**31 - 1
RecordID = Annotated[int, Path(ge=1, le=ID_MAX)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Clock = Annotated[str, StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Repeat = Annotated[str, StringConstraints(min_length=1, max_length=80)]
NO_REPEAT = "Не повторяется"
REPEAT_RULES = {
    NO_REPEAT: None,
    "Повтор каждый день": "FREQ=DAILY",
    "Повтор каждую неделю": "FREQ=WEEKLY",
    "Повтор каждый месяц": "FREQ=MONTHLY",
}
STATUSES = {
    "draft": "Черновик", "queued": "В очереди", "confirmed": "Создано",
    "failed": "Ошибка синхронизации", "declined": "Отклонено",
}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ParseEvent(Input):
    message: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT_LEN)]


class EditEvent(Input):
    title: Title | None = None
    date: Date | None = None
    time: Clock | None = None
    end: Clock | None = None
    repeat: Repeat | None = None


class NewTask(Input):
    title: Title
    date: Date
    time: Clock
    done: StrictBool = False


class EditTask(Input):
    done: StrictBool


class EditNotification(Input):
    unread: StrictBool


class NotificationSettings(Input):
    enabled: StrictBool


def error_response(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message}}, status_code=status,
        headers={**(headers or {}), "Cache-Control": "no-store"},
    )


@api.middleware("http")
async def private_response(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@api.exception_handler(StarletteHTTPException)
async def http_error(_request: Request, exc: StarletteHTTPException):
    return error_response(exc.status_code, exc.detail, exc.headers)


@api.exception_handler(RequestValidationError)
async def invalid_input(_request: Request, _exc: RequestValidationError):
    return error_response(422, "Проверьте поля запроса.")


@api.exception_handler(Exception)
async def internal_error(_request: Request, exc: Exception):
    logger.error("Ошибка API Mini App (%s)", type(exc).__name__)
    return error_response(500, "Не удалось выполнить запрос. Попробуйте ещё раз.")


async def current_user(request: Request) -> User:
    try:
        user_id = verified_user_id(request.headers.get("authorization", ""), bot.token)
    except ValueError:
        raise HTTPException(401, "Откройте Mini App заново из Telegram.") from None

    async with async_session() as session:
        user = await session.scalar(
            select(User).join(Consent, Consent.user_id == User.user_id)
            .where(User.user_id == user_id, Consent.accepted_at.is_not(None), Consent.revoked_at.is_(None))
        )
    if user is None:
        raise HTTPException(403, "Подтвердите согласие и завершите настройку через /start в боте.")
    return user


Owner = Annotated[User, Depends(current_user)]


def zone(name: str | None) -> ZoneInfo:
    offset = re.fullmatch(r"UTC([+-]\d{2}):00", name or "")
    if offset:
        hours = int(offset[1])
        name = f"Etc/GMT{-hours:+d}" if hours else "Etc/UTC"
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


@api.post("/voice/transcribe")
async def voice_transcript(request: Request, user: Owner):
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime not in FORMATS:
        raise HTTPException(415, "Формат записи не поддерживается.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Запись слишком большая. Продиктуйте фразу короче.")
        data.extend(chunk)
    if not data:
        raise HTTPException(422, "Пустая запись. Попробуйте ещё раз.")
    try:
        text = await transcribe_miniapp(bytes(data), FORMATS[mime])
    except SpeechBusy:
        raise HTTPException(429, "Распознавание занято. Повторите через несколько секунд.") from None
    except ValueError as exc:
        raise HTTPException(422, "Не удалось прочитать запись. Продиктуйте короткую фразу ещё раз.") from exc
    if not text.strip():
        raise HTTPException(422, "Речь не распознана. Попробуйте ещё раз.")
    return {"text": text.strip()}


@api.get("/me")
async def me(user: Owner):
    return {"user": {"id": str(user.user_id), "timezone": zone(user.timezone).key}}


def display_times(event: Event, user: User) -> tuple[datetime, datetime]:
    original_zone = zone(event.timezone)
    view_zone = zone(user.timezone)
    start = event.starts_at.replace(tzinfo=original_zone)
    end = event.ends_at or event.starts_at + (event.duration or timedelta(hours=1))
    return start.astimezone(view_zone), end.replace(tzinfo=original_zone).astimezone(view_zone)


def repeat_label(rrule: str | None) -> str:
    if not rrule:
        return NO_REPEAT
    for label, rule in REPEAT_RULES.items():
        if rrule.upper().removeprefix("RRULE:") == rule:
            return label
    return "Повтор по расписанию"


def event_json(event: Event, user: User, chat_title: str | None, operation: str | None) -> dict:
    start, end = display_times(event, user)
    sync = "Создано в Google Calendar" if event.google_event_id else "Не синхронизировано"
    if operation in ("pending", "processing") or event.status == "queued":
        sync = "Ожидает синхронизации"
    elif operation == "failed" or event.status == "failed":
        sync = "Ошибка синхронизации"
    link = event.google_html_link or ""
    return {
        "id": str(event.id), "title": event.title or "Встреча",
        "date": start.date().isoformat(), "time": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
        "chatId": str(event.chat_id) if event.chat_id is not None else None,
        "chat": (chat_title or str(event.chat_id)) if event.chat_id is not None else "",
        "source": "Разрешённый чат Telegram Business" if event.chat_id is not None else "Введено вручную",
        "repeat": repeat_label(event.rrule), "status": STATUSES[event.status], "sync": sync,
        "seriesId": str(event.id) if event.rrule else None, "timezone": zone(event.timezone).key,
        "location": event.location, "participants": event.participants or [],
        "html_link": link if link.startswith("https://calendar.google.com/") else None,
    }


def owned_events(user_id: int):
    return select(Event).where(Event.user_id == user_id, Event.starts_at.is_not(None), Event.status != "deleted")


async def event_metadata(session, event: Event) -> tuple[str | None, str | None]:
    chat_title = None
    if event.chat_id is not None:
        chat_title = await session.scalar(select(Chat.chat_title).where(
            Chat.user_id == event.user_id, Chat.chat_id == event.chat_id,
        ))
    synchronization = await session.scalar(
        select(Operation.status).where(Operation.event_id == event.id).order_by(Operation.id.desc()).limit(1)
    )
    return chat_title, synchronization


@api.get("/events")
async def events(
    user: Owner,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[int | None, Query(ge=1, le=ID_MAX)] = None,
):
    last_status = (
        select(Operation.status).where(Operation.event_id == Event.id)
        .order_by(Operation.id.desc()).limit(1).scalar_subquery()
    )
    statement = (
        owned_events(user.user_id).add_columns(Chat.chat_title, last_status)
        .outerjoin(Chat, and_(Chat.user_id == Event.user_id, Chat.chat_id == Event.chat_id))
        .order_by(Event.id.desc()).limit(limit + 1)
    )
    if cursor is not None:
        statement = statement.where(Event.id < cursor)
    async with async_session() as session:
        rows = (await session.execute(statement)).all()
    return {
        "events": [event_json(event, user, chat, op) for event, chat, op in rows[:limit]],
        "next_cursor": str(rows[limit - 1][0].id) if len(rows) > limit else None,
    }


async def has_google_token(session, user_id: int) -> bool:
    return (await session.scalar(select(Connection.id).where(
        Connection.user_id == user_id, Connection.google_token.is_not(None),
    ))) is not None


def google_event_data(event: Event) -> dict:
    return build_event_data(
        summary=event.title, start_time=event.starts_at.isoformat(), end_time=event.ends_at.isoformat(),
        timezone=zone(event.timezone).key, location=event.location, rrule=event.rrule,
        attendees=[item["name"] for item in event.participants if isinstance(item, dict) and item.get("name")],
    )


@api.post("/events/parse", status_code=201)
async def parse_event(data: ParseEvent, user: Owner):
    if _parse_lock.locked():
        raise HTTPException(429, "Сейчас обрабатывается другое сообщение. Попробуйте ещё раз через несколько секунд.")
    user_zone = zone(user.timezone)
    async with _parse_lock:
        result = await asyncio.to_thread(
            process_dialogue_message, data.message, DialogueState(), tz=user_zone.key,
        )
    record = to_event_record(result, user_id=user.user_id, user_tz=user_zone.key)
    if not record or not record["starts_at"]:
        raise HTTPException(422, result.get("clarify") or "Укажите название, дату и время встречи.")

    event_zone = zone(record["timezone"])
    start = datetime.fromisoformat(record["starts_at"]).astimezone(event_zone).replace(tzinfo=None)
    end = (
        datetime.fromisoformat(record["ends_at"]).astimezone(event_zone).replace(tzinfo=None)
        if record["ends_at"] else start + (record["duration"] or timedelta(hours=1))
    )
    if end <= start:
        raise HTTPException(422, "Конец события должен быть позже начала.")
    event = Event(
        user_id=user.user_id, title=record["title"], starts_at=start, ends_at=end, duration=end - start,
        timezone=event_zone.key, rrule=record["rrule"], location=record["location"],
        participants=record["participants"], status="confirmed",
    )
    async with async_session.begin() as session:
        session.add(event)
        await session.flush()
        queued = await has_google_token(session, user.user_id)
        if queued:
            stage_create(session, event, google_event_data(event), miniapp=True)
    if queued:
        wake_worker()
    return {"event": event_json(event, user, None, "pending" if queued else None)}


def edited_times(event: Event, user: User, data: EditEvent) -> None:
    if data.date is None and data.time is None and data.end is None:
        return
    old_start, old_end = display_times(event, user)
    user_zone = zone(user.timezone)
    event_zone = zone(event.timezone)
    try:
        start = datetime.combine(data.date or old_start.date(),
                                 time.fromisoformat(data.time) if data.time else old_start.time(), user_zone)
        if data.end is None:
            end = start + (old_end - old_start)
        else:
            end = datetime.combine(start.date(), time.fromisoformat(data.end), user_zone)
            if end < start and old_start.date() != old_end.date():
                end += timedelta(days=1)
        if end <= start:
            raise HTTPException(422, "Конец события должен быть позже начала.")
        event.starts_at = start.astimezone(event_zone).replace(tzinfo=None)
        event.ends_at = end.astimezone(event_zone).replace(tzinfo=None)
        event.duration = event.ends_at - event.starts_at
    except OverflowError:
        raise HTTPException(422, "Дата события вне допустимого диапазона.") from None


def ensure_event_editable(event: Event | None) -> Event:
    if event is None:
        raise HTTPException(404, "Событие не найдено.")
    if event.status == "queued":
        raise HTTPException(409, "Событие синхронизируется с Google. Обновите приложение и повторите.")
    return event


@api.patch("/events/{event_id}")
async def edit_event(event_id: RecordID, data: EditEvent, user: Owner):
    queued = False
    async with async_session.begin() as session:
        event = ensure_event_editable(await session.scalar(
            owned_events(user.user_id).where(Event.id == event_id).with_for_update()
        ))
        chat, op = await event_metadata(session, event)
        if data.title is not None:
            event.title = data.title
        edited_times(event, user, data)
        repeat_changed = data.repeat is not None and data.repeat != repeat_label(event.rrule)
        if repeat_changed:
            if data.repeat not in REPEAT_RULES:
                raise HTTPException(422, "Выберите поддерживаемое повторение.")
            event.rrule = REPEAT_RULES[data.repeat]

        if data.model_dump(exclude_none=True):
            connected = await has_google_token(session, user.user_id)
            if event.google_event_id:
                if not connected:
                    raise HTTPException(409, "Подключите Google Календарь, чтобы изменить событие.")
                end = event.ends_at or event.starts_at + (event.duration or timedelta(hours=1))
                payload = {
                    "miniapp": True, "google_event_id": event.google_event_id,
                    "new_start_time": event.starts_at.isoformat(), "new_end_time": end.isoformat(),
                    "timezone": zone(event.timezone).key, "summary": event.title or "Встреча",
                }
                if repeat_changed or op == "failed":
                    payload["recurrence"] = ["RRULE:" + event.rrule.removeprefix("RRULE:")] if event.rrule else []
                stage_operation(session, user.user_id, "update", payload, event.id)
                queued = True
            elif connected and event.chat_id is None and event.status in ("confirmed", "failed"):
                if not event.ends_at:
                    event.ends_at = event.starts_at + (event.duration or timedelta(hours=1))
                stage_create(session, event, google_event_data(event), miniapp=True)
                queued = True
    if queued:
        wake_worker()
    return {"event": event_json(event, user, chat, "pending" if queued else op)}


async def remove_event(event_id: int, user: User, *, series: bool) -> Response:
    async with async_session.begin() as session:
        event = ensure_event_editable(await session.scalar(
            owned_events(user.user_id).where(Event.id == event_id).with_for_update()
        ))
        if bool(event.rrule) != series:
            raise HTTPException(409, "Для повторяющегося события отмените серию." if event.rrule
                                else "У события нет серии.")
        queued = bool(event.google_event_id)
        if queued:
            if not await has_google_token(session, user.user_id):
                raise HTTPException(409, "Подключите Google Календарь, чтобы удалить событие.")
            stage_operation(session, user.user_id, "delete", {
                "miniapp": True, "google_event_id": event.google_event_id, "previous_status": event.status,
            }, event.id)
        event.status = "deleted"
    if queued:
        wake_worker()
    return Response(status_code=204)


@api.delete("/events/{event_id}", status_code=204)
async def delete_event(event_id: RecordID, user: Owner):
    return await remove_event(event_id, user, series=False)


@api.delete("/events/{event_id}/series", status_code=204)
async def delete_series(event_id: RecordID, user: Owner):
    return await remove_event(event_id, user, series=True)


def task_json(task: Task) -> dict:
    return {
        "id": str(task.id), "title": task.title, "date": task.date.isoformat(),
        "time": task.time.strftime("%H:%M"), "done": task.done,
    }


@api.get("/tasks")
async def tasks(user: Owner):
    async with async_session() as session:
        records = (await session.scalars(select(Task).where(Task.user_id == user.user_id)
                                         .order_by(Task.date, Task.time, Task.id))).all()
    return {"tasks": [task_json(task) for task in records]}


@api.post("/tasks", status_code=201)
async def create_task(data: NewTask, user: Owner):
    task = Task(user_id=user.user_id, title=data.title, date=data.date, time=time.fromisoformat(data.time), done=data.done)
    async with async_session.begin() as session:
        session.add(task)
        await session.flush()
    return {"task": task_json(task)}


async def locked_task(session, task_id: int, user_id: int) -> Task:
    task = await session.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id).with_for_update())
    if task is None:
        raise HTTPException(404, "Задача не найдена.")
    return task


@api.patch("/tasks/{task_id}")
async def update_task(task_id: RecordID, data: EditTask, user: Owner):
    async with async_session.begin() as session:
        task = await locked_task(session, task_id, user.user_id)
        task.done = data.done
    return {"task": task_json(task)}


@api.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: RecordID, user: Owner):
    async with async_session.begin() as session:
        await session.delete(await locked_task(session, task_id, user.user_id))
    return Response(status_code=204)


def notice_kind(event: Event) -> str:
    return "recognized" if event.chat_id is not None and event.status == "draft" else "event"


@api.get("/notifications")
async def notifications(user: Owner):
    statement = owned_events(user.user_id).where(Event.status != "declined").order_by(Event.id.desc()).limit(100)
    async with async_session() as session:
        records = (await session.scalars(statement)).all()
        reads = set((await session.execute(
            select(EventNoticeRead.event_id, EventNoticeRead.kind)
            .where(EventNoticeRead.event_id.in_([event.id for event in records]))
        )).all())
    notifications = []
    for event in records:
        kind = notice_kind(event)
        title = {
            "recognized": "Распознано новое событие",
            "event": "Событие в истории",
        }[kind]
        notifications.append({
            "id": f"{kind}-{event.id}", "kind": kind, "title": title, "text": event.title or "Встреча",
            "extra": STATUSES[event.status], "unread": (event.id, kind) not in reads, "eventId": str(event.id),
        })
    return {"notifications": notifications, "enabled": user.notification is not False}


@api.patch("/notifications/{notification_id}")
async def read_notification(
    notification_id: Annotated[str, Path(pattern=r"^(?:event|recognized)-[0-9]{1,10}$")],
    data: EditNotification,
    user: Owner,
):
    kind, number = notification_id.split("-")
    event_id = int(number)
    if event_id > ID_MAX or data.unread:
        raise HTTPException(422, "Уведомление можно отметить прочитанным.")
    async with async_session.begin() as session:
        event = await session.scalar(owned_events(user.user_id).where(Event.id == event_id))
        if event is None or event.status == "declined" or (kind == "recognized" and event.chat_id is None):
            raise HTTPException(404, "Уведомление не найдено.")
        await session.execute(insert(EventNoticeRead).values(event_id=event.id, kind=kind).on_conflict_do_nothing())
    return {"unread": False}


@api.put("/notifications/settings")
async def change_notification_settings(data: NotificationSettings, user: Owner):
    async with async_session.begin() as session:
        saved = await session.get(User, user.id, with_for_update=True)
        if saved is None:
            raise HTTPException(404, "Профиль не найден.")
        saved.notification = data.enabled
    return {"enabled": data.enabled}


@api.get("/google/status")
async def google_status(user: Owner):
    storage = DBTokenStorage()
    if not await storage.get_user_token(user.user_id):
        return {"connected": False}
    try:
        result = await asyncio.wait_for(check_connection(user.user_id, storage), timeout=3)
    except TimeoutError:
        return {"connected": True}
    return {"connected": result.get("error_type") != "auth_error"}


@api.post("/google/connect")
async def google_connect(user: Owner):
    try:
        url = oauth.build_auth_url(user.user_id)
    except RuntimeError:
        raise HTTPException(503, "Подключение Google Календаря пока недоступно.") from None
    return {"authorization_url": url}


@api.delete("/google/connection", status_code=204)
async def google_disconnect(user: Owner):
    oauth.cancel_auth(user.user_id)
    await DBTokenStorage().revoke_user_token(user.user_id)
    return Response(status_code=204)


@api.get("/chats")
async def chats(user: Owner):
    async with async_session() as session:
        token = await session.scalar(select(Connection.business_token).where(Connection.user_id == user.user_id))
    if not token:
        return {"scope": "unknown", "chats": []}
    try:
        grant = await bot.get_business_connection(token, request_timeout=5)
    except Exception as error:
        logger.info("Не удалось проверить Telegram Business (%s)", type(error).__name__)
        return {"scope": "unknown", "chats": []}
    if grant.id != token or not grant.is_enabled or grant.user.id != user.user_id:
        return {"scope": "unknown", "chats": []}
    async with async_session() as session:
        rows = (await session.scalars(select(Chat).where(
            Chat.user_id == user.user_id, Chat.chat_id.is_not(None),
        ).order_by(Chat.id))).all()
    return {"scope": "observed", "chats": [{"id": str(row.chat_id), "title": row.chat_title} for row in rows]}
