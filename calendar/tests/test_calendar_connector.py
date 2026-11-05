import sys
from pathlib import Path

# Гарантируем, что pytest видит модули в корневой папке проекта
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from httplib2 import Response

from calendar_connector import (
    CalendarAuthError,
    create_event,
    delete_event,
    get_calendar_service,
    update_event_time,
)
from rrule_builder import build_rrule


PRIMARY_CALENDAR_ID = "primary"


class FakeDBTokenStorage:
    """Мок-хранилище БД для изолированного тестирования без реальной PostgreSQL."""

    def __init__(self, initial_tokens: dict[int, dict] | None = None):
        self.db = initial_tokens or {}
        self.saved_calls = 0

    def get_user_token(self, user_id: int) -> dict | None:
        return self.db.get(user_id)

    def save_user_token(self, user_id: int, token_data: dict) -> None:
        self.db[user_id] = token_data
        self.saved_calls += 1


def make_http_error(status: int, reason: str = "Error") -> HttpError:
    """Вспомогательная фабрика объектов HttpError библиотеки googleapiclient."""
    resp = Response({"status": status, "reason": reason})
    return HttpError(resp=resp, content=b'{"error": {"message": "Mocked error"}}')


def get_valid_event_data() -> dict:
    """Возвращает корректный минимальный набор данных для создания события."""
    return {
        "summary": "Тестовая встреча",
        "start_time": "2026-11-01T10:00:00",
        "end_time": "2026-11-01T11:00:00",
        "timezone": "Europe/Stockholm",
    }


# ---------------------------------------------------------------------------
# Авторизация и работа с токенами
# ---------------------------------------------------------------------------


def test_missing_token_in_db_returns_auth_error():
    """Если токена пользователя нет в БД, возвращается статус ошибки auth_error."""
    storage = FakeDBTokenStorage()

    res = create_event(
        user_id=999,
        token_storage=storage,
        event_data=get_valid_event_data(),
    )

    assert res["status"] == "error"
    assert res["error_type"] == "auth_error"
    assert res["need_queue"] is False


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
def test_expired_token_refreshes_and_saves_to_db(mock_from_info, mock_build):
    """Если токен истёк, он должен обновиться через Request и сохраниться обратно в БД."""
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "valid_refresh_token"
    mock_creds.to_json.return_value = '{"token": "new_access_token"}'
    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({101: {"token": "old_token"}})

    get_calendar_service(user_id=101, token_storage=storage)

    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 1
    assert storage.db[101] == {"token": "new_access_token"}


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
def test_invalid_token_format_returns_auth_error(mock_from_info, mock_build):
    """Повреждённый JSON токена в БД должен приводить к auth_error без обращения к Google API."""
    mock_from_info.side_effect = ValueError("Invalid token format")

    storage = FakeDBTokenStorage({102: {"broken": "token"}})

    with pytest.raises(CalendarAuthError):
        get_calendar_service(user_id=102, token_storage=storage)


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
def test_refresh_error_raises_calendar_auth_error(mock_from_info, mock_build):
    """
    Если Google отклонил refresh_token, должно возникнуть CalendarAuthError,
    а обновлённый токен не должен сохраняться в БД.
    """
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "expired_refresh_token"
    mock_creds.refresh.side_effect = RefreshError("Token has been expired or revoked")

    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({1: {"token": "old_access_token"}})

    with pytest.raises(CalendarAuthError) as exc_info:
        get_calendar_service(user_id=1, token_storage=storage)

    assert "Токен доступа отозван или устарел" in str(exc_info.value)
    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 0
    assert storage.db[1] == {"token": "old_access_token"}


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
def test_transport_error_during_token_refresh_is_raised(mock_from_info, mock_build):
    """
    Сетевая ошибка TransportError при обновлении токена должна быть проброшена дальше:
    это временный сбой, а не ошибка авторизации.
    """
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "valid_refresh_token"
    mock_creds.refresh.side_effect = TransportError("Network connection failed")

    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({1: {"token": "old_access_token"}})

    with pytest.raises(TransportError):
        get_calendar_service(user_id=1, token_storage=storage)

    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 0


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
def test_os_error_during_token_refresh_is_raised(mock_from_info, mock_build):
    """Сетевой сбой OSError при обновлении токена должен быть проброшен дальше."""
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "valid_refresh_token"
    mock_creds.refresh.side_effect = OSError("Connection reset by peer")

    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({1: {"token": "old_access_token"}})

    with pytest.raises(OSError):
        get_calendar_service(user_id=1, token_storage=storage)

    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 0


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
def test_invalid_token_without_refresh_token(mock_from_info, mock_build):
    """
    Если токен невалиден, истёк и не содержит refresh_token,
    должно возникнуть CalendarAuthError.
    """
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = None

    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({1: {"token": "old_access_token"}})

    with pytest.raises(CalendarAuthError) as exc_info:
        get_calendar_service(user_id=1, token_storage=storage)

    assert "Невалидный токен доступа" in str(exc_info.value)
    mock_creds.refresh.assert_not_called()
    assert storage.saved_calls == 0


# ---------------------------------------------------------------------------
# Валидация обязательных полей события
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    ["summary", "start_time", "end_time"],
)
@patch("calendar_connector.get_calendar_service")
def test_create_event_missing_required_field_returns_validation_error(
    mock_get_service,
    missing_field,
):
    """Отсутствие любого обязательного поля должно возвращать validation_error."""
    event_data = get_valid_event_data()
    event_data.pop(missing_field)

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=event_data,
    )

    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    assert res["need_queue"] is False
    assert missing_field in res["message"]
    mock_get_service.assert_not_called()


# ---------------------------------------------------------------------------
# Создание события
# ---------------------------------------------------------------------------


@patch("calendar_connector.get_calendar_service")
def test_create_event_uses_primary_calendar(mock_get_service):
    """Разовое событие должно создаваться только в основном календаре пользователя."""
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.return_value = {
        "id": "evt_001",
        "htmlLink": "https://calendar.google.com/event?eid=001",
    }
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )

    assert res["status"] == "success"
    assert res["event_id"] == "evt_001"
    assert res["html_link"] == "https://calendar.google.com/event?eid=001"
    assert "calendar_id" not in res

    mock_service.events.return_value.insert.assert_called_once_with(
        calendarId=PRIMARY_CALENDAR_ID,
        body={
            "summary": "Тестовая встреча",
            "description": "Создано ботом ДЕЛУ.ВРЕМЯ",
            "start": {
                "dateTime": "2026-11-01T10:00:00",
                "timeZone": "Europe/Stockholm",
            },
            "end": {
                "dateTime": "2026-11-01T11:00:00",
                "timeZone": "Europe/Stockholm",
            },
        },
    )


@patch("calendar_connector.get_calendar_service")
def test_create_recurring_event_uses_rrule_and_primary_calendar(mock_get_service):
    """Повторяющееся событие должно содержать RRULE и создаваться в primary-календаре."""
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.return_value = {
        "id": "evt_rrule",
        "htmlLink": "https://calendar.google.com/event?eid=rrule",
    }
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    event_data = get_valid_event_data()
    event_data["recurrence"] = {
        "freq": "WEEKLY",
        "interval": 1,
        "byday": ["MO", "WE"],
        "count": 5,
    }

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=event_data,
    )

    assert res["status"] == "success"
    assert res["event_id"] == "evt_rrule"

    call_args = mock_service.events.return_value.insert.call_args
    assert call_args.kwargs["calendarId"] == PRIMARY_CALENDAR_ID
    assert "recurrence" in call_args.kwargs["body"]
    assert call_args.kwargs["body"]["recurrence"][0].startswith("RRULE:")


@patch("calendar_connector.get_calendar_service")
def test_create_event_with_attendees(mock_get_service):
    """При наличии участников они должны быть добавлены в тело запроса к Google Calendar API."""
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.return_value = {
        "id": "evt_attendees",
        "htmlLink": "https://calendar.google.com/event?eid=attendees",
    }
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    event_data = get_valid_event_data()
    event_data["attendees"] = ["  user1@example.com  ", "", "user2@example.com"]

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=event_data,
    )

    assert res["status"] == "success"

    call_args = mock_service.events.return_value.insert.call_args
    assert call_args.kwargs["calendarId"] == PRIMARY_CALENDAR_ID
    assert call_args.kwargs["body"]["attendees"] == [
        {"email": "user1@example.com"},
        {"email": "user2@example.com"},
    ]


@patch("calendar_connector.get_calendar_service")
def test_create_event_empty_api_response_returns_success_with_none_event_id(
    mock_get_service,
):
    """
    Если Google API вернул пустой ответ, модуль возвращает success,
    но event_id и html_link остаются None.
    """
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.return_value = None
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )

    assert res["status"] == "success"
    assert res["event_id"] is None
    assert res["html_link"] is None


@patch("calendar_connector.get_calendar_service")
def test_create_event_unexpected_error_returns_validation_error(mock_get_service):
    """
    Любая неожиданная ошибка при формировании или отправке события
    должна быть обработана как validation_error.
    """
    mock_get_service.side_effect = RuntimeError("Unexpected internal error")

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )

    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    assert res["need_queue"] is False
    assert "Unexpected internal error" in res["message"]


@patch("calendar_connector.get_calendar_service")
def test_retry_on_503_and_primary_calendar_success(mock_get_service):
    """При двух сбоях 503 подряд и успехе на 3-й попытке событие создаётся в primary-календаре."""
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.side_effect = [
        make_http_error(503, "Service Unavailable"),
        make_http_error(503, "Service Unavailable"),
        {"id": "evt_777", "htmlLink": "https://calendar.google.com/event?eid=777"},
    ]
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )

    assert res["status"] == "success"
    assert res["event_id"] == "evt_777"
    assert "calendar_id" not in res
    assert mock_insert_req.execute.call_count == 3

    mock_service.events.return_value.insert.assert_called_once_with(
        calendarId=PRIMARY_CALENDAR_ID,
        body={
            "summary": "Тестовая встреча",
            "description": "Создано ботом ДЕЛУ.ВРЕМЯ",
            "start": {
                "dateTime": "2026-11-01T10:00:00",
                "timeZone": "Europe/Stockholm",
            },
            "end": {
                "dateTime": "2026-11-01T11:00:00",
                "timeZone": "Europe/Stockholm",
            },
        },
    )


@pytest.mark.parametrize("status", [429, 500, 503])
@patch("calendar_connector.get_calendar_service")
def test_retryable_http_errors_flag_event_for_queue(mock_get_service, status):
    """Ошибки 429, 500 и 503 должны повторяться три раза и помечать событие для очереди."""
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.side_effect = make_http_error(status)
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )

    assert res["status"] == "error"
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_insert_req.execute.call_count == 3


@patch("calendar_connector.get_calendar_service")
def test_network_error_is_retried_and_queued(mock_get_service):
    """Сетевой сбой должен повторяться три раза и помечать операцию для очереди."""
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.side_effect = OSError("Network connection lost")
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    res = create_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )

    assert res["status"] == "error"
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_insert_req.execute.call_count == 3


# ---------------------------------------------------------------------------
# Перенос события
# ---------------------------------------------------------------------------


@patch("calendar_connector.get_calendar_service")
def test_update_event_time_uses_primary_calendar(mock_get_service):
    """Перенос события должен выполняться в primary-календаре."""
    mock_service = MagicMock()
    mock_patch_req = MagicMock()
    mock_patch_req.execute.return_value = {"id": "evt_100"}
    mock_service.events.return_value.patch.return_value = mock_patch_req
    mock_get_service.return_value = mock_service

    res = update_event_time(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="evt_100",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
        timezone="Europe/Stockholm",
    )

    assert res["status"] == "success"
    assert res["event_id"] == "evt_100"
    assert "calendar_id" not in res

    mock_service.events.return_value.patch.assert_called_once_with(
        calendarId=PRIMARY_CALENDAR_ID,
        eventId="evt_100",
        body={
            "start": {
                "dateTime": "2026-11-03T14:00:00",
                "timeZone": "Europe/Stockholm",
            },
            "end": {
                "dateTime": "2026-11-03T15:00:00",
                "timeZone": "Europe/Stockholm",
            },
        },
    )


@patch("calendar_connector.get_calendar_service")
def test_update_event_time_empty_api_response(mock_get_service):
    """Если Google API вернул пустой ответ при переносе, event_id в результате будет None."""
    mock_service = MagicMock()
    mock_patch_req = MagicMock()
    mock_patch_req.execute.return_value = None
    mock_service.events.return_value.patch.return_value = mock_patch_req
    mock_get_service.return_value = mock_service

    res = update_event_time(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="evt_100",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
    )

    assert res["status"] == "success"
    assert res["event_id"] is None


@pytest.mark.parametrize(
    "event_id, start_time, end_time",
    [
        ("", "2026-11-03T14:00:00", "2026-11-03T15:00:00"),
        ("evt_100", "", "2026-11-03T15:00:00"),
        ("evt_100", "2026-11-03T14:00:00", ""),
    ],
)
@patch("calendar_connector.get_calendar_service")
def test_update_event_time_missing_fields_returns_validation_error(
    mock_get_service,
    event_id,
    start_time,
    end_time,
):
    """Отсутствие event_id, времени начала или времени окончания должно давать validation_error."""
    res = update_event_time(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id=event_id,
        new_start_time=start_time,
        new_end_time=end_time,
    )

    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    assert res["need_queue"] is False
    mock_get_service.assert_not_called()


@patch("calendar_connector.get_calendar_service")
def test_update_event_time_unexpected_error_returns_validation_error(
    mock_get_service,
):
    """Неожиданная ошибка при переносе события должна возвращать validation_error."""
    mock_get_service.side_effect = RuntimeError("Unexpected update error")

    res = update_event_time(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="evt_100",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    assert res["need_queue"] is False
    assert "Unexpected update error" in res["message"]


@patch("calendar_connector.get_calendar_service")
def test_update_event_time_404_error_does_not_retry(mock_get_service):
    """Клиентская ошибка 404 не должна вызывать повторные попытки."""
    mock_service = MagicMock()
    mock_patch_req = MagicMock()
    mock_patch_req.execute.side_effect = make_http_error(404, "Not Found")
    mock_service.events.return_value.patch.return_value = mock_patch_req
    mock_get_service.return_value = mock_service

    res = update_event_time(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="non_existent_id",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "api_error"
    assert res["need_queue"] is False
    assert mock_patch_req.execute.call_count == 1


@patch("calendar_connector.get_calendar_service")
def test_update_event_time_network_error_is_retried(mock_get_service):
    """Сетевой сбой при переносе события должен повторяться три раза."""
    mock_service = MagicMock()
    mock_patch_req = MagicMock()
    mock_patch_req.execute.side_effect = OSError("Connection reset")
    mock_service.events.return_value.patch.return_value = mock_patch_req
    mock_get_service.return_value = mock_service

    res = update_event_time(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="evt_100",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_patch_req.execute.call_count == 3


# ---------------------------------------------------------------------------
# Удаление события
# ---------------------------------------------------------------------------


@patch("calendar_connector.get_calendar_service")
def test_delete_event_uses_primary_calendar(mock_get_service):
    """Удаление события должно выполняться в primary-календаре."""
    mock_service = MagicMock()
    mock_delete_req = MagicMock()
    mock_delete_req.execute.return_value = ""
    mock_service.events.return_value.delete.return_value = mock_delete_req
    mock_get_service.return_value = mock_service

    res = delete_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="evt_200",
    )

    assert res["status"] == "success"
    assert "calendar_id" not in res

    mock_service.events.return_value.delete.assert_called_once_with(
        calendarId=PRIMARY_CALENDAR_ID,
        eventId="evt_200",
    )


@patch("calendar_connector.get_calendar_service")
def test_delete_event_missing_event_id_returns_validation_error(mock_get_service):
    """Попытка удалить событие без event_id должна возвращать validation_error."""
    res = delete_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    assert res["need_queue"] is False
    mock_get_service.assert_not_called()


@patch("calendar_connector.get_calendar_service")
def test_delete_event_unexpected_error_returns_validation_error(mock_get_service):
    """Неожиданная ошибка при удалении события должна возвращать validation_error."""
    mock_get_service.side_effect = RuntimeError("Unexpected delete error")

    res = delete_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="evt_200",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    assert res["need_queue"] is False
    assert "Unexpected delete error" in res["message"]


@patch("calendar_connector.get_calendar_service")
def test_delete_event_403_forbidden(mock_get_service):
    """При попытке удалить событие без прав доступа ошибка перехватывается сразу."""
    mock_service = MagicMock()
    mock_delete_req = MagicMock()
    mock_delete_req.execute.side_effect = make_http_error(403, "Forbidden")
    mock_service.events.return_value.delete.return_value = mock_delete_req
    mock_get_service.return_value = mock_service

    res = delete_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="locked_event_id",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "api_error"
    assert res["need_queue"] is False
    assert mock_delete_req.execute.call_count == 1


@patch("calendar_connector.get_calendar_service")
def test_delete_event_network_error_is_retried(mock_get_service):
    """Сетевой сбой при удалении события должен повторяться три раза."""
    mock_service = MagicMock()
    mock_delete_req = MagicMock()
    mock_delete_req.execute.side_effect = OSError("Timeout")
    mock_service.events.return_value.delete.return_value = mock_delete_req
    mock_get_service.return_value = mock_service

    res = delete_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="evt_200",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_delete_req.execute.call_count == 3


# ---------------------------------------------------------------------------
# RRULE
# ---------------------------------------------------------------------------


def test_rrule_validation_errors():
    """Проверка защиты генератора RRULE от некорректных параметров."""
    with pytest.raises(ValueError, match="Неподдерживаемая частота"):
        build_rrule(freq="HOURLY")

    with pytest.raises(ValueError, match="взаимоисключающие"):
        build_rrule(freq="WEEKLY", count=5, until="20261231")

    with pytest.raises(ValueError, match="Некорректные дни недели"):
        build_rrule(freq="WEEKLY", byday=["MO", "MONDAY"])
