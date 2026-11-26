"""
Коннектор Google Calendar API v3.
Требования ТЗ: F-B-09, F-B-10, F-B-16, F-B-17, F-B-18, NF-07, NF-08, NF-29.

Правки относительно исходной версии (логика ядра сохранена):
  * async/await для TokenStorage — обязательное условие интеграции с async-БД;
  * service.close() в finally (не текут сокеты httplib2);
  * идемпотентный event_body["id"] — защита от дублей при retry (у Google API нет Idempotency-Key);
  * 409 Conflict трактуется как idempotent success;
  * creds.refresh НЕ ретраится локально: сетевой сбой при refresh -> need_queue=True,
    повтор выполняется на уровне очереди (NF-07, NF-08);
  * все блокирующие sync-вызовы (build, refresh, execute, close) выполняются через asyncio.to_thread;
  * ValueError -> validation_error (без очереди), прочие неожиданные ошибки -> internal_error (в очередь);
  * TZ по умолчанию "Europe/Moscow"; вызывающий код ОБЯЗАН передавать event_data["timezone"]
    из User.timezone (иначе будут неверные UNTIL для recurring-событий).
"""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from calendarr.rrule_builder import build_rrule

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
PRIMARY_CALENDAR_ID = "primary"
DEFAULT_TIMEZONE = "Europe/Moscow"

NETWORK_EXCEPTIONS = (HttpError, httplib2.HttpLib2Error, TransportError, OSError)


class CalendarAuthError(Exception):
    """Нет/невалиден OAuth-токен пользователя — требуется повторная авторизация."""


class TokenStorageProtocol(Protocol):
    """
    Контракт с БД. Реализация обязана:
      * хранить токен в зашифрованном виде (AES-256 / Fernet — согласуется с командой);
      * возвращать dict в формате `google.oauth2.credentials.Credentials.to_json()`;
      * быть async (asyncpg + SQLAlchemy AsyncSession).
    """
    async def get_user_token(self, user_id: int) -> dict[str, Any] | None: ...
    async def save_user_token(self, user_id: int, token_data: dict[str, Any]) -> None: ...


def _is_retryable_error(exc: BaseException) -> bool:
    """NF-07: повторяем только временные сбои (429, 5xx, сетевые обрывы)."""
    if isinstance(exc, HttpError):
        return exc.resp.status in (429, 500, 502, 503, 504)
    return isinstance(exc, (httplib2.HttpLib2Error, TransportError, OSError))


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception(_is_retryable_error),
)
def _execute_request(request_callable) -> dict[str, Any] | None:
    try:
        return request_callable.execute()
    except NETWORK_EXCEPTIONS as err:
        if _is_retryable_error(err):
            logger.warning("Временный сбой Google API (%s), повторная попытка", type(err).__name__)
        raise


def _refresh_credentials(creds: Credentials) -> None:
    """
    Обновляет access_token. Сетевые сбои здесь пробрасываются наверх —
    `create_event` / `update_event_time` / `delete_event` ловят их как
    NETWORK_EXCEPTIONS и возвращают need_queue=True (NF-07, NF-08).
    Повторная попытка — на уровне очереди, а не здесь.
    """
    creds.refresh(Request())


async def get_calendar_service(user_id: int, token_storage: TokenStorageProtocol):
    token_data = await token_storage.get_user_token(user_id)
    if not token_data or not isinstance(token_data, dict):
        logger.warning("Токен для user_id=%s не найден в БД.", user_id)
        raise CalendarAuthError("Пользователь не авторизован в Google Calendar.")

    try:
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    except (ValueError, TypeError, KeyError) as err:
        logger.error("Повреждённый формат токена в БД у user_id=%s", user_id)
        raise CalendarAuthError(
            "Некорректный формат токена в БД. Требуется повторная авторизация."
        ) from err

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                logger.info("Обновление истёкшего токена для user_id=%s", user_id)
                await asyncio.to_thread(_refresh_credentials, creds)
                fresh = json.loads(creds.to_json())
                refresh = getattr(token_storage, "refresh_user_token", None)
                if refresh is None:
                    await token_storage.save_user_token(user_id, fresh)
                elif not await refresh(user_id, token_data, fresh):
                    current = await token_storage.get_user_token(user_id)
                    if not current or current.get("refresh_token") != token_data.get("refresh_token"):
                        raise CalendarAuthError("Google Календарь был отключён или переподключён. Повторите запрос.")
                    creds = Credentials.from_authorized_user_info(current, SCOPES)
            except RefreshError as err:
                logger.error("Токен отозван или устарел у user_id=%s", user_id)
                raise CalendarAuthError(
                    "Токен доступа отозван или устарел. Требуется повторный вход."
                ) from err
        else:
            logger.error("Невалидный токен без refresh_token у user_id=%s", user_id)
            raise CalendarAuthError(
                "Невалидный токен доступа. Требуется повторная авторизация."
            )

    return await asyncio.to_thread(
        build, "calendar", "v3", credentials=creds, cache_discovery=False
    )


def _validate_required_event_fields(event_data: dict[str, Any]) -> None:
    required = ("summary", "start_time", "end_time")
    missing = [k for k in required if not event_data.get(k)]
    if missing:
        raise ValueError(f"Отсутствуют обязательные поля события: {', '.join(missing)}")


def _validate_time_range(start: str, end: str) -> None:
    """end должен быть строго позже start (если оба значения tz-aware / оба naive)."""
    try:
        start_dt = datetime.fromisoformat(str(start))
        end_dt = datetime.fromisoformat(str(end))
    except ValueError as err:
        raise ValueError("Некорректный формат даты или времени") from err
    start_aware = start_dt.tzinfo is not None
    end_aware = end_dt.tzinfo is not None
    if start_aware != end_aware:
        # Смешение aware/naive сравнивать нельзя — считаем naive в одном поясе с событием.
        start_dt = start_dt.replace(tzinfo=None)
        end_dt = end_dt.replace(tzinfo=None)
    if end_dt <= start_dt:
        raise ValueError("Время окончания должно быть позже времени начала")


def _resolve_rrule(event_data: dict[str, Any], timezone: str) -> str | None:
    """Готовая строка event_data['rrule'] приоритетнее dict event_data['recurrence']."""
    raw = event_data.get("rrule")
    if raw:
        rule = str(raw).strip()
        if not rule.upper().startswith("RRULE:"):
            rule = "RRULE:" + rule
        if "FREQ=" not in rule.upper():
            raise ValueError("В правиле повторения отсутствует FREQ")
        return rule
    rec = event_data.get("recurrence")
    if rec:
        return build_rrule(
            freq=rec.get("freq", ""),
            interval=rec.get("interval", 1),
            byday=rec.get("byday"),
            count=rec.get("count"),
            until=rec.get("until"),
            timezone=timezone,
        )
    return None


def _build_idempotent_event_id(user_id: int, event_data: dict[str, Any]) -> str:
    """
    Детерминированный UUIDv5 в hex (Google допускает в id только base32hex: a-v, 0-9).
    Повторная отправка того же события даст 409 — трактуем как «уже создано».
    """
    if "google_event_id" in event_data:
        value = event_data["google_event_id"]
        if not isinstance(value, str) or not re.fullmatch(r"[a-v0-9]{5,1024}", value):
            raise ValueError("Некорректный идентификатор события Google Calendar")
        return value

    if event_data.get("rrule"):
        rec_key = str(event_data["rrule"]).strip()
    elif event_data.get("recurrence"):
        rec_key = json.dumps(event_data["recurrence"], sort_keys=True, ensure_ascii=False)
    else:
        rec_key = ""
    key = "|".join([
        str(user_id),
        str(event_data.get("summary", "")),
        str(event_data.get("start_time", "")),
        str(event_data.get("end_time", "")),
        rec_key,
    ])
    return uuid.uuid5(uuid.NAMESPACE_URL, key).hex


async def _close_service(service) -> None:
    if service is None:
        return
    try:
        await asyncio.to_thread(service.close)
    except Exception:  # noqa: BLE001
        pass


def _error_result(error_type: str, message: str, need_queue: bool) -> dict[str, Any]:
    return {"status": "error", "error_type": error_type,
            "message": message, "need_queue": need_queue}


def _api_error_result(api_err: BaseException, context: str) -> dict[str, Any]:
    is_unavailable = _is_retryable_error(api_err)
    detail = api_err.resp.status if isinstance(api_err, HttpError) else type(api_err).__name__
    logger.error("Сбой Google API %s (%s)", context, detail)
    message = "Google Calendar временно недоступен" if is_unavailable else "Ошибка Google Calendar"
    return _error_result(
        "api_unavailable" if is_unavailable else "api_error", message, is_unavailable
    )


async def create_event(
        user_id: int,
        token_storage: TokenStorageProtocol,
        event_data: dict[str, Any],
) -> dict[str, Any]:
    """F-B-09 (разовое) + F-B-10 (RRULE)."""
    service = None
    try:
        # Работаем с копией — словарь вызывающего кода не мутируем.
        event_data = dict(event_data)

        # Автоподстановка времени окончания: start + duration_minutes (по умолчанию 60 мин)
        if not event_data.get("end_time") and event_data.get("start_time"):
            try:
                start_dt = datetime.fromisoformat(str(event_data["start_time"]))
                duration = int(event_data.get("duration_minutes") or 60)
                event_data["end_time"] = (start_dt + timedelta(minutes=duration)).isoformat()
            except (ValueError, TypeError):
                pass  # Если формат кривой, пусть падает на валидации ниже

        _validate_required_event_fields(event_data)
        _validate_time_range(event_data["start_time"], event_data["end_time"])
        timezone = event_data.get("timezone") or DEFAULT_TIMEZONE
        rrule_string = _resolve_rrule(event_data, timezone)
        event_id = _build_idempotent_event_id(user_id, event_data)

        service = await get_calendar_service(user_id=user_id, token_storage=token_storage)

        event_body: dict[str, Any] = {
            "id": event_id,
            "summary": str(event_data["summary"]).strip(),
            "description": event_data.get("description", "Создано ботом ДЕЛУ.ВРЕМЯ"),
            "start": {"dateTime": event_data["start_time"], "timeZone": timezone},
            "end": {"dateTime": event_data["end_time"], "timeZone": timezone},
        }
        if event_data.get("location"):
            event_body["location"] = str(event_data["location"]).strip()
        if event_data.get("attendees"):
            event_body["attendees"] = [
                {"email": str(m).strip()} for m in event_data["attendees"] if m
            ]
        if rrule_string:
            event_body["recurrence"] = [rrule_string]

        try:
            req = service.events().insert(calendarId=PRIMARY_CALENDAR_ID, body=event_body)
            created = await asyncio.to_thread(_execute_request, req) or {}
        except HttpError as api_err:
            if api_err.resp.status != 409:
                raise
            return await _handle_conflict(service, event_body)

        return {
            "status": "success",
            "event_id": created.get("id") or event_body["id"],
            "html_link": created.get("htmlLink"),
            "duplicate": False,
        }

    except CalendarAuthError as err:
        return _error_result("auth_error", str(err), False)
    except ValueError as err:
        logger.error("Ошибка валидации при создании события")
        return _error_result("validation_error", str(err), False)
    except NETWORK_EXCEPTIONS as api_err:
        return _api_error_result(api_err, "при создании события")
    except Exception as err:  # noqa: BLE001
        logger.error("Внутренняя ошибка при создании события (%s)", type(err).__name__)
        return _error_result("internal_error", "Внутренняя ошибка создания события", True)
    finally:
        await _close_service(service)


async def _handle_conflict(service, event_body: dict[str, Any]) -> dict[str, Any]:
    """
    409 на insert: id уже занят. Удалённое событие в Google хранится со status=cancelled
    и остаётся невидимым — тогда восстанавливаем его через update.
    """
    event_id = event_body["id"]
    get_req = service.events().get(calendarId=PRIMARY_CALENDAR_ID, eventId=event_id)
    existing = await asyncio.to_thread(_execute_request, get_req) or {}
    if existing.get("status") == "cancelled":
        restore_body = {**event_body, "status": "confirmed"}
        upd_req = service.events().update(
            calendarId=PRIMARY_CALENDAR_ID, eventId=event_id, body=restore_body
        )
        restored = await asyncio.to_thread(_execute_request, upd_req) or {}
        return {
            "status": "success",
            "event_id": restored.get("id") or event_id,
            "html_link": restored.get("htmlLink"),
            "duplicate": False,
        }
    return {
        "status": "success",
        "event_id": existing.get("id") or event_id,
        "html_link": existing.get("htmlLink"),
        "duplicate": True,
    }


async def update_event_time(
    user_id: int,
    token_storage: TokenStorageProtocol,
    event_id: str,
    new_start_time: str,
    new_end_time: str,
    timezone: str = DEFAULT_TIMEZONE,
        *,
        summary: str | None = None,
        recurrence: list[str] | None = None,
) -> dict[str, Any]:
    """F-B-17, F-M-11. ВНИМАНИЕ: PATCH применяется ко всей серии повторений."""
    service = None
    try:
        if not event_id or not new_start_time or not new_end_time:
            raise ValueError("Не указаны event_id, new_start_time или new_end_time")
        _validate_time_range(new_start_time, new_end_time)

        service = await get_calendar_service(user_id=user_id, token_storage=token_storage)
        patch_body = {
            "start": {"dateTime": new_start_time, "timeZone": timezone},
            "end": {"dateTime": new_end_time, "timeZone": timezone},
        }
        if summary is not None:
            patch_body["summary"] = summary
        if recurrence is not None:
            patch_body["recurrence"] = recurrence
        req = service.events().patch(
            calendarId=PRIMARY_CALENDAR_ID, eventId=event_id, body=patch_body
        )
        updated = await asyncio.to_thread(_execute_request, req) or {}
        return {"status": "success", "event_id": updated.get("id") or event_id}

    except CalendarAuthError as err:
        return _error_result("auth_error", str(err), False)
    except ValueError as err:
        logger.error("Ошибка валидации при переносе события")
        return _error_result("validation_error", str(err), False)
    except NETWORK_EXCEPTIONS as api_err:
        return _api_error_result(api_err, "при переносе события")
    except Exception as err:  # noqa: BLE001
        logger.error("Внутренняя ошибка при переносе события (%s)", type(err).__name__)
        return _error_result("internal_error", "Внутренняя ошибка переноса события", True)
    finally:
        await _close_service(service)


async def delete_event(
    user_id: int,
    token_storage: TokenStorageProtocol,
    event_id: str,
) -> dict[str, Any]:
    """F-B-16, F-B-18, F-M-10, F-M-12. Удаляет событие или всю серию."""
    service = None
    try:
        if not event_id:
            raise ValueError("Не указан event_id для удаления")

        service = await get_calendar_service(user_id=user_id, token_storage=token_storage)
        req = service.events().delete(calendarId=PRIMARY_CALENDAR_ID, eventId=event_id)
        try:
            await asyncio.to_thread(_execute_request, req)
        except HttpError as api_err:
            if api_err.resp.status in (404, 410):
                return {"status": "success", "already_deleted": True}
            raise
        return {"status": "success"}

    except CalendarAuthError as err:
        return _error_result("auth_error", str(err), False)
    except ValueError as err:
        logger.error("Ошибка валидации при удалении события")
        return _error_result("validation_error", str(err), False)
    except NETWORK_EXCEPTIONS as api_err:
        return _api_error_result(api_err, "при удалении события")
    except Exception as err:  # noqa: BLE001
        logger.error("Внутренняя ошибка при удалении события (%s)", type(err).__name__)
        return _error_result("internal_error", "Внутренняя ошибка удаления события", True)
    finally:
        await _close_service(service)


async def check_connection(
    user_id: int,
    token_storage: TokenStorageProtocol,
) -> dict[str, Any]:
    """Проверяет, что токен рабочий: лёгкий запрос к основному календарю
    (events.list, maxResults=1 — хватает scope calendar.events).

    Ответ в том же формате, что у create_event / delete_event:
    {"status": "success"} или {"status": "error", "error_type", "message", "need_queue"},
    где error_type="auth_error" — токена нет, он испорчен, отозван или устарел.
    """
    service = None
    try:
        service = await get_calendar_service(user_id=user_id, token_storage=token_storage)
        req = service.events().list(calendarId=PRIMARY_CALENDAR_ID, maxResults=1)
        try:
            await asyncio.to_thread(_execute_request, req)
        except HttpError as api_err:
            if api_err.resp.status in (401, 403):  # доступ отозван в настройках Google-аккаунта
                logger.warning("Google отклонил токен user_id=%s: %s", user_id, api_err)
                return _error_result("auth_error", str(api_err), False)
            raise
        return {"status": "success"}

    except CalendarAuthError as err:
        return _error_result("auth_error", str(err), False)
    except RefreshError as err:  # access-токен истёк во время запроса, а refresh_token отозван
        logger.warning("Токен отозван/устарел у user_id=%s: %s", user_id, err)
        return _error_result("auth_error", str(err), False)
    except NETWORK_EXCEPTIONS as api_err:
        return _api_error_result(api_err, f"при проверке подключения user_id={user_id}")
    except Exception as err:  # noqa: BLE001
        logger.exception("Внутренняя ошибка при проверке подключения user_id=%s", user_id)
        return _error_result("internal_error", str(err), True)
    finally:
        await _close_service(service)
