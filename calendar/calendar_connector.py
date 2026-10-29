import json
import logging
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

# Кортеж сетевых исключений, которые могут возникнуть при работе с Google API
NETWORK_EXCEPTIONS = (HttpError, httplib2.HttpLib2Error, TransportError, OSError)


class CalendarAuthError(Exception):
    """Исключение при отсутствии или невалидности OAuth-токена пользователя в БД."""


class TokenStorageProtocol(Protocol):
    """
    Протокол взаимодействия с БД (PostgreSQL).
    Реализация на стороне БД шифрует/дешифрует токены алгоритмом AES-256 (NF-10).
    """

    def get_user_token(self, user_id: int) -> dict[str, Any] | None:
        """Возвращает расшифрованный JSON-словарь токена пользователя из БД."""
        ...

    def save_user_token(self, user_id: int, token_data: dict[str, Any]) -> None:
        """Шифрует (AES-256) и сохраняет обновлённый токен пользователя в БД."""
        ...


def _is_retryable_error(exc: BaseException) -> bool:
    """
    Определяет, является ли ошибка временным сетевым сбоем или перегрузкой сервера Google.
    Повторяем запрос при HTTP 429, 500, 502, 503, 504 и любых сетевых обрывах (OSError / TransportError).
    """
    if isinstance(exc, HttpError):
        return exc.resp.status in (429, 500, 502, 503, 504)
    return isinstance(exc, (httplib2.HttpLib2Error, TransportError, OSError))


# Выполнение требования NF-07: >= 3 попыток с экспоненциальной задержкой
@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception(_is_retryable_error),
)
def _execute_request(request_callable) -> dict[str, Any] | None:
    """Универсальная обёртка для отправки запросов в Google API с защитой от сбоев."""
    try:
        return request_callable.execute()
    except NETWORK_EXCEPTIONS as err:
        if _is_retryable_error(err):
            logger.warning(f"Временный сбой при запросе к Google API ({err}), повторная попытка...")
        raise


def get_calendar_service(user_id: int, token_storage: TokenStorageProtocol):
    """
    Загружает токен пользователя из БД, при необходимости обновляет его через refresh_token
    и возвращает инициализированный клиент Google Calendar API v3 (NF-29).
    """
    token_data = token_storage.get_user_token(user_id)
    if not token_data or not isinstance(token_data, dict):
        logger.warning(f"Токен для пользователя user_id={user_id} не найден в БД.")
        raise CalendarAuthError("Пользователь не авторизован в Google Calendar.")

    try:
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    except (ValueError, TypeError, KeyError) as err:
        logger.error(f"Повреждённый формат токена в БД у user_id={user_id}: {err}")
        raise CalendarAuthError("Некорректный формат токена в БД. Требуется повторная авторизация.") from err

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                logger.info(f"Обновление истёкшего токена для user_id={user_id}")
                creds.refresh(Request())
                token_storage.save_user_token(user_id, json.loads(creds.to_json()))
            except RefreshError as err:
                logger.error(f"Токен отозван или не подлежит обновлению для user_id={user_id}: {err}")
                raise CalendarAuthError("Токен доступа отозван или устарел. Требуется повторный вход.") from err
            except (TransportError, OSError) as net_err:
                logger.error(f"Сетевой сбой при обновлении токена для user_id={user_id}: {net_err}")
                raise
        else:
            logger.error(f"Невалидный токен без refresh_token у user_id={user_id}")
            raise CalendarAuthError("Невалидный токен доступа. Требуется повторная авторизация.")

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _validate_required_event_fields(event_data: dict[str, Any]) -> None:
    """Проверяет наличие обязательных полей события перед формированием запроса."""
    required_keys = ("summary", "start_time", "end_time")
    missing = [key for key in required_keys if not event_data.get(key)]
    if missing:
        raise ValueError(f"Отсутствуют обязательные поля события: {', '.join(missing)}")


def create_event(
    user_id: int,
    token_storage: TokenStorageProtocol,
    event_data: dict[str, Any],
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """
    Создаёт разовое (F-B-09) или повторяющееся событие через RRULE (F-B-10) в выбранном календаре.
    """
    try:
        _validate_required_event_fields(event_data)
        service = get_calendar_service(user_id=user_id, token_storage=token_storage)
        timezone = event_data.get("timezone") or "Europe/Moscow"

        event_body: dict[str, Any] = {
            "summary": str(event_data["summary"]).strip(),
            "description": event_data.get("description", "Создано ботом ДЕЛУ.ВРЕМЯ"),
            "start": {"dateTime": event_data["start_time"], "timeZone": timezone},
            "end": {"dateTime": event_data["end_time"], "timeZone": timezone},
        }

        if event_data.get("attendees"):
            event_body["attendees"] = [{"email": str(mail).strip()} for mail in event_data["attendees"] if mail]

        if event_data.get("recurrence"):
            rec = event_data["recurrence"]
            rrule_string = build_rrule(
                freq=rec.get("freq", ""),
                interval=rec.get("interval", 1),
                byday=rec.get("byday"),
                count=rec.get("count"),
                until=rec.get("until"),
            )
            event_body["recurrence"] = [rrule_string]
            logger.info(f"Сформировано правило повторения для user_id={user_id}: {rrule_string}")

        logger.info(f"Отправка события '{event_body['summary']}' в календарь '{calendar_id}' (user_id={user_id})")
        req = service.events().insert(calendarId=calendar_id, body=event_body)
        created_event = _execute_request(req) or {}

        return {
            "status": "success",
            "event_id": created_event.get("id"),
            "html_link": created_event.get("htmlLink"),
            "calendar_id": calendar_id,
        }

    except CalendarAuthError as auth_err:
        return {"status": "error", "error_type": "auth_error", "message": str(auth_err), "need_queue": False}
    except NETWORK_EXCEPTIONS as api_err:
        logger.error(f"Сбой Google API при создании события для user_id={user_id}: {api_err}")
        is_unavailable = _is_retryable_error(api_err)
        return {
            "status": "error",
            "error_type": "api_unavailable" if is_unavailable else "api_error",
            "message": str(api_err),
            "need_queue": is_unavailable,
        }
    except Exception as err:
        logger.error(f"Ошибка валидации данных при создании события: {err}")
        return {"status": "error", "error_type": "validation_error", "message": str(err), "need_queue": False}


def update_event_time(
    user_id: int,
    token_storage: TokenStorageProtocol,
    event_id: str,
    new_start_time: str,
    new_end_time: str,
    timezone: str = "Europe/Moscow",
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """Переносит существующую встречу на новое время (F-B-17, F-M-11)."""
    try:
        if not event_id or not new_start_time or not new_end_time:
            raise ValueError("Не указаны event_id, new_start_time или new_end_time для переноса события")

        service = get_calendar_service(user_id=user_id, token_storage=token_storage)
        patch_body = {
            "start": {"dateTime": new_start_time, "timeZone": timezone},
            "end": {"dateTime": new_end_time, "timeZone": timezone},
        }
        req = service.events().patch(calendarId=calendar_id, eventId=event_id, body=patch_body)
        updated = _execute_request(req) or {}
        logger.info(f"Время события {event_id} в календаре '{calendar_id}' успешно изменено.")
        return {"status": "success", "event_id": updated.get("id"), "calendar_id": calendar_id}

    except CalendarAuthError as auth_err:
        return {"status": "error", "error_type": "auth_error", "message": str(auth_err), "need_queue": False}
    except NETWORK_EXCEPTIONS as api_err:
        logger.error(f"Ошибка API при переносе события {event_id} в календаре '{calendar_id}': {api_err}")
        is_unavailable = _is_retryable_error(api_err)
        return {
            "status": "error",
            "error_type": "api_unavailable" if is_unavailable else "api_error",
            "message": str(api_err),
            "need_queue": is_unavailable,
        }
    except Exception as error:
        logger.error(f"Ошибка валидации при переносе события {event_id}: {error}")
        return {"status": "error", "error_type": "validation_error", "message": str(error), "need_queue": False}


def delete_event(
    user_id: int,
    token_storage: TokenStorageProtocol,
    event_id: str,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """Удаляет разовое событие или всю серию повторений по его ID (F-B-16, F-B-18, F-M-10, F-M-12)."""
    try:
        if not event_id:
            raise ValueError("Не указан event_id для удаления события")

        service = get_calendar_service(user_id=user_id, token_storage=token_storage)
        req = service.events().delete(calendarId=calendar_id, eventId=event_id)
        _execute_request(req)
        logger.info(f"Событие {event_id} из календаря '{calendar_id}' успешно удалено.")
        return {"status": "success", "calendar_id": calendar_id}

    except CalendarAuthError as auth_err:
        return {"status": "error", "error_type": "auth_error", "message": str(auth_err), "need_queue": False}
    except NETWORK_EXCEPTIONS as api_err:
        logger.error(f"Ошибка API при удалении события {event_id} из календаря '{calendar_id}': {api_err}")
        is_unavailable = _is_retryable_error(api_err)
        return {
            "status": "error",
            "error_type": "api_unavailable" if is_unavailable else "api_error",
            "message": str(api_err),
            "need_queue": is_unavailable,
        }
    except Exception as error:
        logger.error(f"Ошибка валидации при удалении события {event_id}: {error}")
        return {"status": "error", "error_type": "validation_error", "message": str(error), "need_queue": False}
