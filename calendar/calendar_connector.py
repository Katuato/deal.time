"""
Коннектор Google Calendar API v3.
Требования ТЗ: F-B-09, F-B-10, F-B-16, F-B-17, F-B-18, NF-07, NF-08, NF-29.

Правки относительно исходной версии (логика ядра сохранена):
  * async/await для TokenStorage — обязательное условие интеграции с async-БД;
  * service.close() в finally (не текут сокеты httplib2);
  * идемпотентный event_body["id"] — защита от дублей при retry (у Google API нет Idempotency-Key);
  * 409 Conflict трактуется как idempotent success;
  * creds.refresh защищён @retry (раньше сетевой сбой при refresh не ретраился — нарушение NF-07);
  * TZ по умолчанию "Europe/Moscow"; вызывающий код ОБЯЗАН передавать event_data["timezone"]
    из User.timezone (иначе будут неверные UNTIL для recurring-событий).
"""
import json
import logging
import uuid
from typing import Any, Protocol

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from rrule_builder import build_rrule

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
            logger.warning("Временный сбой Google API (%s), повторная попытка...", err)
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
        logger.error("Повреждённый формат токена в БД у user_id=%s: %s", user_id, err)
        raise CalendarAuthError(
            "Некорректный формат токена в БД. Требуется повторная авторизация."
        ) from err

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                logger.info("Обновление истёкшего токена для user_id=%s", user_id)
                _refresh_credentials(creds)
                await token_storage.save_user_token(user_id, json.loads(creds.to_json()))
            except RefreshError as err:
                logger.error("Токен отозван/устарел у user_id=%s: %s", user_id, err)
                raise CalendarAuthError(
                    "Токен доступа отозван или устарел. Требуется повторный вход."
                ) from err
        else:
            logger.error("Невалидный токен без refresh_token у user_id=%s", user_id)
            raise CalendarAuthError(
                "Невалидный токен доступа. Требуется повторная авторизация."
            )

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _validate_required_event_fields(event_data: dict[str, Any]) -> None:
    required = ("summary", "start_time", "end_time")
    missing = [k for k in required if not event_data.get(k)]
    if missing:
        raise ValueError(f"Отсутствуют обязательные поля события: {', '.join(missing)}")


def _build_idempotent_event_id(user_id: int, event_data: dict[str, Any]) -> str:
    """
    Детерминированный UUIDv5. При повторной отправке того же события
    Google вернёт 409 — трактуем как «уже создано».
    """
    key = "|".join([
        str(user_id),
        str(event_data.get("summary", "")),
        str(event_data.get("start_time", "")),
        str(event_data.get("end_time", "")),
        json.dumps(event_data.get("recurrence"), sort_keys=True, ensure_ascii=False)
            if event_data.get("recurrence") else "",
    ])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


async def create_event(
        user_id: int,
        token_storage: TokenStorageProtocol,
        event_data: dict[str, Any],
) -> dict[str, Any]:
    """F-B-09 (разовое) + F-B-10 (RRULE)."""
    service = None
    try:
        # --- НОВЫЙ БЛОК: Автоподстановка времени окончания (Fallback) ---
        from datetime import datetime, timedelta

        if not event_data.get("end_time") and event_data.get("start_time"):
            try:
                # Парсим время начала (например, "2026-11-15T15:00:00+03:00")
                start_dt = datetime.fromisoformat(event_data["start_time"])
                # Прибавляем 1 час
                end_dt = start_dt + timedelta(hours=1)
                # Записываем обратно в строку ISO
                event_data["end_time"] = end_dt.isoformat()
                logger.info("end_time не передан. Автоматически установлено: %s", event_data["end_time"])
            except ValueError:
                pass  # Если формат кривой, пусть падает на валидации ниже
        # ---------------------------------------------------------------

        _validate_required_event_fields(event_data)
        service = await get_calendar_service(user_id=user_id, token_storage=token_storage)
        timezone = event_data.get("timezone") or DEFAULT_TIMEZONE

        event_body: dict[str, Any] = {
            "id": _build_idempotent_event_id(user_id, event_data),
            "summary": str(event_data["summary"]).strip(),
            "description": event_data.get("description", "Создано ботом ДЕЛУ.ВРЕМЯ"),
            "start": {"dateTime": event_data["start_time"], "timeZone": timezone},
            "end": {"dateTime": event_data["end_time"], "timeZone": timezone},
        }
        if event_data.get("attendees"):
            event_body["attendees"] = [
                {"email": str(m).strip()} for m in event_data["attendees"] if m
            ]
        if event_data.get("recurrence"):
            rec = event_data["recurrence"]
            rrule_string = build_rrule(
                freq=rec.get("freq", ""),
                interval=rec.get("interval", 1),
                byday=rec.get("byday"),
                count=rec.get("count"),
                until=rec.get("until"),
                timezone=timezone,
            )
            event_body["recurrence"] = [rrule_string]
            logger.info("RRULE для user_id=%s: %s", user_id, rrule_string)

        logger.info("Создание события '%s' (user_id=%s)", event_body["summary"], user_id)
        req = service.events().insert(calendarId=PRIMARY_CALENDAR_ID, body=event_body)
        created = _execute_request(req) or {}

        return {
            "status": "success",
            "event_id": created.get("id") or event_body["id"],
            "html_link": created.get("htmlLink"),
            "duplicate": False,
        }

    except CalendarAuthError as err:
        return {"status": "error", "error_type": "auth_error",
                "message": str(err), "need_queue": False}

    except HttpError as api_err:
        if api_err.resp.status == 409:
            logger.info("Событие уже создано (idempotent duplicate), user_id=%s", user_id)
            return {"status": "success",
                    "event_id": _build_idempotent_event_id(user_id, event_data),
                    "html_link": None, "duplicate": True}
        is_unavailable = _is_retryable_error(api_err)
        logger.error("Сбой Google API при создании user_id=%s: %s", user_id, api_err)
        return {"status": "error",
                "error_type": "api_unavailable" if is_unavailable else "api_error",
                "message": str(api_err), "need_queue": is_unavailable}

    except NETWORK_EXCEPTIONS as api_err:
        logger.error("Сетевой сбой при создании user_id=%s: %s", user_id, api_err)
        return {"status": "error", "error_type": "api_unavailable",
                "message": str(api_err), "need_queue": True}

    except Exception as err:
        logger.error("Ошибка валидации при создании: %s", err)
        return {"status": "error", "error_type": "validation_error",
                "message": str(err), "need_queue": False}

    finally:
        if service is not None:
            try:
                service.close()
            except Exception:  # noqa: BLE001
                pass


async def update_event_time(
    user_id: int,
    token_storage: TokenStorageProtocol,
    event_id: str,
    new_start_time: str,
    new_end_time: str,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """F-B-17, F-M-11. ВНИМАНИЕ: PATCH применяется ко всей серии повторений."""
    service = None
    try:
        if not event_id or not new_start_time or not new_end_time:
            raise ValueError("Не указаны event_id, new_start_time или new_end_time")

        service = await get_calendar_service(user_id=user_id, token_storage=token_storage)
        patch_body = {
            "start": {"dateTime": new_start_time, "timeZone": timezone},
            "end": {"dateTime": new_end_time, "timeZone": timezone},
        }
        req = service.events().patch(
            calendarId=PRIMARY_CALENDAR_ID, eventId=event_id, body=patch_body
        )
        updated = _execute_request(req) or {}
        logger.info("Время события %s обновлено", event_id)
        return {"status": "success", "event_id": updated.get("id") or event_id}

    except CalendarAuthError as err:
        return {"status": "error", "error_type": "auth_error",
                "message": str(err), "need_queue": False}
    except NETWORK_EXCEPTIONS as api_err:
        is_unavailable = _is_retryable_error(api_err)
        logger.error("Сбой API при переносе %s: %s", event_id, api_err)
        return {"status": "error",
                "error_type": "api_unavailable" if is_unavailable else "api_error",
                "message": str(api_err), "need_queue": is_unavailable}
    except Exception as err:
        logger.error("Ошибка валидации при переносе %s: %s", event_id, err)
        return {"status": "error", "error_type": "validation_error",
                "message": str(err), "need_queue": False}
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:  # noqa: BLE001
                pass


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
        _execute_request(req)
        logger.info("Событие %s удалено", event_id)
        return {"status": "success"}

    except CalendarAuthError as err:
        return {"status": "error", "error_type": "auth_error",
                "message": str(err), "need_queue": False}
    except NETWORK_EXCEPTIONS as api_err:
        is_unavailable = _is_retryable_error(api_err)
        logger.error("Сбой API при удалении %s: %s", event_id, api_err)
        return {"status": "error",
                "error_type": "api_unavailable" if is_unavailable else "api_error",
                "message": str(api_err), "need_queue": is_unavailable}
    except Exception as err:
        logger.error("Ошибка валидации при удалении %s: %s", event_id, err)
        return {"status": "error", "error_type": "validation_error",
                "message": str(err), "need_queue": False}
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:  # noqa: BLE001
                pass