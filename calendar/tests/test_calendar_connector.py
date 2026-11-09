import sys
from pathlib import Path

# calendar/tests → calendar/ (чтобы импортировать calendar_connector и rrule_builder)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import AsyncMock, MagicMock, patch

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

from tenacity import wait_none
from calendar_connector import _execute_request

@pytest.fixture(autouse=True)
def _fast_retries_in_tests(monkeypatch):
    """
    В тестах обнуляем задержки между retry, чтобы файл прогонялся за <1с.
    В production код `wait_exponential` (NF-07) не меняется.
    """
    monkeypatch.setattr(_execute_request.retry, "wait", wait_none())


PRIMARY_CALENDAR_ID = "primary"


class FakeDBTokenStorage:
    """Async-мок хранилища — БД не требуется."""
    def __init__(self, initial_tokens: dict[int, dict] | None = None):
        self.db = initial_tokens or {}
        self.saved_calls = 0

    async def get_user_token(self, user_id: int) -> dict | None:
        return self.db.get(user_id)

    async def save_user_token(self, user_id: int, token_data: dict) -> None:
        self.db[user_id] = token_data
        self.saved_calls += 1


def make_http_error(status: int, reason: str = "Error") -> HttpError:
    resp = Response({"status": status, "reason": reason})
    return HttpError(resp=resp, content=b'{"error": {"message": "Mocked error"}}')


def get_valid_event_data() -> dict:
    return {
        "summary": "Тестовая встреча",
        "start_time": "2026-11-01T10:00:00",
        "end_time": "2026-11-01T11:00:00",
        "timezone": "Europe/Stockholm",
    }


# ---------------- Авторизация и работа с токенами ----------------

async def test_missing_token_in_db_returns_auth_error():
    storage = FakeDBTokenStorage()
    res = await create_event(
        user_id=999, token_storage=storage, event_data=get_valid_event_data(),
    )
    assert res["status"] == "error"
    assert res["error_type"] == "auth_error"
    assert res["need_queue"] is False


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
async def test_expired_token_refreshes_and_saves_to_db(mock_from_info, mock_build):
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "valid_refresh_token"
    mock_creds.to_json.return_value = '{"token": "new_access_token"}'
    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({101: {"token": "old_token"}})
    await get_calendar_service(user_id=101, token_storage=storage)

    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 1
    assert storage.db[101] == {"token": "new_access_token"}


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
async def test_invalid_token_format_returns_auth_error(mock_from_info, mock_build):
    mock_from_info.side_effect = ValueError("Invalid token format")
    storage = FakeDBTokenStorage({102: {"broken": "token"}})

    with pytest.raises(CalendarAuthError):
        await get_calendar_service(user_id=102, token_storage=storage)


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
async def test_refresh_error_raises_calendar_auth_error(mock_from_info, mock_build):
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "expired_refresh_token"
    mock_creds.refresh.side_effect = RefreshError("Token has been expired or revoked")
    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({1: {"token": "old_access_token"}})
    with pytest.raises(CalendarAuthError) as exc_info:
        await get_calendar_service(user_id=1, token_storage=storage)

    assert "Токен доступа отозван или устарел" in str(exc_info.value)
    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 0
    assert storage.db[1] == {"token": "old_access_token"}


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
async def test_transport_error_during_token_refresh_is_raised(mock_from_info, mock_build):
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "valid_refresh_token"
    mock_creds.refresh.side_effect = TransportError("Network connection failed")
    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({1: {"token": "old_access_token"}})
    with pytest.raises(TransportError):
        await get_calendar_service(user_id=1, token_storage=storage)

    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 0


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
async def test_os_error_during_token_refresh_is_raised(mock_from_info, mock_build):
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "valid_refresh_token"
    mock_creds.refresh.side_effect = OSError("Connection reset by peer")
    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({1: {"token": "old_access_token"}})
    with pytest.raises(OSError):
        await get_calendar_service(user_id=1, token_storage=storage)

    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 0


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
async def test_invalid_token_without_refresh_token(mock_from_info, mock_build):
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = None
    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({1: {"token": "old_access_token"}})
    with pytest.raises(CalendarAuthError) as exc_info:
        await get_calendar_service(user_id=1, token_storage=storage)

    assert "Невалидный токен доступа" in str(exc_info.value)
    mock_creds.refresh.assert_not_called()
    assert storage.saved_calls == 0


# ---------------- Валидация обязательных полей ----------------

@pytest.mark.parametrize("missing_field", ["summary", "start_time", "end_time"])
@patch("calendar_connector.get_calendar_service")
async def test_create_event_missing_required_field_returns_validation_error(
    mock_get_service, missing_field,
):
    event_data = get_valid_event_data()
    event_data.pop(missing_field)

    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(), event_data=event_data,
    )
    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    assert res["need_queue"] is False
    assert missing_field in res["message"]
    mock_get_service.assert_not_called()


# ---------------- Создание события ----------------

@patch("calendar_connector.get_calendar_service")
async def test_create_event_uses_primary_calendar(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = {
        "id": "evt_001",
        "htmlLink": "https://calendar.google.com/event?eid=001",
    }
    mock_service.events.return_value.insert.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )
    assert res["status"] == "success"
    assert res["event_id"] == "evt_001"
    assert res["html_link"] == "https://calendar.google.com/event?eid=001"
    assert res["duplicate"] is False

    kwargs = mock_service.events.return_value.insert.call_args.kwargs
    assert kwargs["calendarId"] == PRIMARY_CALENDAR_ID
    body = kwargs["body"]
    assert body["summary"] == "Тестовая встреча"
    assert body["start"] == {
        "dateTime": "2026-11-01T10:00:00", "timeZone": "Europe/Stockholm",
    }
    assert body["end"] == {
        "dateTime": "2026-11-01T11:00:00", "timeZone": "Europe/Stockholm",
    }
    assert "id" in body and len(body["id"]) == 36  # UUIDv5


@patch("calendar_connector.get_calendar_service")
async def test_create_recurring_event_uses_rrule(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = {"id": "evt_rrule", "htmlLink": "..."}
    mock_service.events.return_value.insert.return_value = mock_req
    mock_get_service.return_value = mock_service

    event_data = get_valid_event_data()
    event_data["recurrence"] = {
        "freq": "WEEKLY", "interval": 1, "byday": ["MO", "WE"], "count": 5,
    }
    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(), event_data=event_data,
    )
    assert res["status"] == "success"
    body = mock_service.events.return_value.insert.call_args.kwargs["body"]
    assert body["recurrence"][0].startswith("RRULE:")


@patch("calendar_connector.get_calendar_service")
async def test_create_event_with_attendees(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = {"id": "evt_att", "htmlLink": "..."}
    mock_service.events.return_value.insert.return_value = mock_req
    mock_get_service.return_value = mock_service

    event_data = get_valid_event_data()
    event_data["attendees"] = ["  user1@example.com  ", "", "user2@example.com"]
    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(), event_data=event_data,
    )
    assert res["status"] == "success"
    body = mock_service.events.return_value.insert.call_args.kwargs["body"]
    assert body["attendees"] == [
        {"email": "user1@example.com"}, {"email": "user2@example.com"},
    ]


@patch("calendar_connector.get_calendar_service")
async def test_create_event_empty_api_response_returns_success(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = None
    mock_service.events.return_value.insert.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )
    assert res["status"] == "success"
    # id генерится детерминированно на нашей стороне
    assert res["event_id"] is not None
    assert res["html_link"] is None


@patch("calendar_connector.get_calendar_service")
async def test_create_event_409_conflict_is_idempotent_duplicate(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(409, "Conflict")
    mock_service.events.return_value.insert.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )
    assert res["status"] == "success"
    assert res["duplicate"] is True
    assert len(res["event_id"]) == 36


@patch("calendar_connector.get_calendar_service")
async def test_create_event_unexpected_error_returns_validation_error(mock_get_service):
    mock_get_service.side_effect = RuntimeError("Unexpected internal error")
    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )
    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    assert "Unexpected internal error" in res["message"]


@patch("calendar_connector.get_calendar_service")
async def test_retry_on_503_succeeds_on_third_attempt(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = [
        make_http_error(503), make_http_error(503),
        {"id": "evt_777", "htmlLink": "..."},
    ]
    mock_service.events.return_value.insert.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )
    assert res["status"] == "success"
    assert mock_req.execute.call_count == 3


@pytest.mark.parametrize("status", [429, 500, 503])
@patch("calendar_connector.get_calendar_service")
async def test_retryable_http_errors_flag_event_for_queue(mock_get_service, status):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(status)
    mock_service.events.return_value.insert.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )
    assert res["status"] == "error"
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_req.execute.call_count == 3


@patch("calendar_connector.get_calendar_service")
async def test_network_error_is_retried_and_queued(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = OSError("Network connection lost")
    mock_service.events.return_value.insert.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await create_event(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_data=get_valid_event_data(),
    )
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_req.execute.call_count == 3


# ---------------- Перенос события ----------------

@patch("calendar_connector.get_calendar_service")
async def test_update_event_time_uses_primary_calendar(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = {"id": "evt_100"}
    mock_service.events.return_value.patch.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await update_event_time(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_id="evt_100",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
        timezone="Europe/Stockholm",
    )
    assert res["status"] == "success"
    kwargs = mock_service.events.return_value.patch.call_args.kwargs
    assert kwargs["calendarId"] == PRIMARY_CALENDAR_ID
    assert kwargs["eventId"] == "evt_100"


@patch("calendar_connector.get_calendar_service")
async def test_update_event_time_empty_api_response(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = None
    mock_service.events.return_value.patch.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await update_event_time(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_id="evt_100",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
    )
    assert res["status"] == "success"
    assert res["event_id"] == "evt_100"


@pytest.mark.parametrize("event_id,start,end", [
    ("", "2026-11-03T14:00:00", "2026-11-03T15:00:00"),
    ("evt_100", "", "2026-11-03T15:00:00"),
    ("evt_100", "2026-11-03T14:00:00", ""),
])
@patch("calendar_connector.get_calendar_service")
async def test_update_event_missing_fields(mock_get_service, event_id, start, end):
    res = await update_event_time(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_id=event_id, new_start_time=start, new_end_time=end,
    )
    assert res["status"] == "error"
    assert res["error_type"] == "validation_error"
    mock_get_service.assert_not_called()


@patch("calendar_connector.get_calendar_service")
async def test_update_event_time_unexpected_error(mock_get_service):
    mock_get_service.side_effect = RuntimeError("Unexpected update error")
    res = await update_event_time(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_id="evt_100",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
    )
    assert res["error_type"] == "validation_error"
    assert "Unexpected update error" in res["message"]


@patch("calendar_connector.get_calendar_service")
async def test_update_event_404_does_not_retry(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(404)
    mock_service.events.return_value.patch.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await update_event_time(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_id="missing",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
    )
    assert res["error_type"] == "api_error"
    assert res["need_queue"] is False
    assert mock_req.execute.call_count == 1


@patch("calendar_connector.get_calendar_service")
async def test_update_event_time_network_error_is_retried(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = OSError("Connection reset")
    mock_service.events.return_value.patch.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await update_event_time(
        user_id=1, token_storage=FakeDBTokenStorage(),
        event_id="evt_100",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
    )
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_req.execute.call_count == 3


# ---------------- Удаление события ----------------

@patch("calendar_connector.get_calendar_service")
async def test_delete_event_uses_primary_calendar(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.return_value = ""
    mock_service.events.return_value.delete.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await delete_event(
        user_id=1, token_storage=FakeDBTokenStorage(), event_id="evt_200",
    )
    assert res["status"] == "success"
    kwargs = mock_service.events.return_value.delete.call_args.kwargs
    assert kwargs["calendarId"] == PRIMARY_CALENDAR_ID
    assert kwargs["eventId"] == "evt_200"


@patch("calendar_connector.get_calendar_service")
async def test_delete_event_missing_id(mock_get_service):
    res = await delete_event(
        user_id=1, token_storage=FakeDBTokenStorage(), event_id="",
    )
    assert res["error_type"] == "validation_error"
    mock_get_service.assert_not_called()


@patch("calendar_connector.get_calendar_service")
async def test_delete_event_unexpected_error(mock_get_service):
    mock_get_service.side_effect = RuntimeError("Unexpected delete error")
    res = await delete_event(
        user_id=1, token_storage=FakeDBTokenStorage(), event_id="evt_200",
    )
    assert res["error_type"] == "validation_error"
    assert "Unexpected delete error" in res["message"]


@patch("calendar_connector.get_calendar_service")
async def test_delete_event_403_forbidden(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = make_http_error(403)
    mock_service.events.return_value.delete.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await delete_event(
        user_id=1, token_storage=FakeDBTokenStorage(), event_id="locked",
    )
    assert res["error_type"] == "api_error"
    assert res["need_queue"] is False
    assert mock_req.execute.call_count == 1


@patch("calendar_connector.get_calendar_service")
async def test_delete_event_network_error_is_retried(mock_get_service):
    mock_service = MagicMock()
    mock_req = MagicMock()
    mock_req.execute.side_effect = OSError("Timeout")
    mock_service.events.return_value.delete.return_value = mock_req
    mock_get_service.return_value = mock_service

    res = await delete_event(
        user_id=1, token_storage=FakeDBTokenStorage(), event_id="evt_200",
    )
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_req.execute.call_count == 3


# ---------------- RRULE ----------------

def test_rrule_validation_errors():
    with pytest.raises(ValueError, match="Неподдерживаемая частота"):
        build_rrule(freq="HOURLY")
    with pytest.raises(ValueError, match="взаимоисключающие"):
        build_rrule(freq="WEEKLY", count=5, until="20261231")
    with pytest.raises(ValueError, match="Некорректные дни недели"):
        build_rrule(freq="WEEKLY", byday=["MO", "MONDAY"])