import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from calendar_connector import create_event


# 1. Имитируем базу данных
class FakeStorage:
    async def get_user_token(self, user_id):
        return {
            "token": "fake", "refresh_token": "fake", "client_id": "fake",
            "client_secret": "fake", "token_uri": "fake"
        }

    async def save_user_token(self, user_id, token_data):
        pass


# 2. Имитируем данные от NLP-модуля Анны
event_data_from_anna = {
    "summary": "Встреча с командой",
    "start_time": "2026-11-15T15:00:00+03:00",
    "end_time": "2026-11-15T16:00:00+03:00",
    "timezone": "Europe/Moscow",
    "recurrence": {"freq": "WEEKLY", "byday": ["FR"], "count": 5}
}


async def main():
    print("--- Проверка контракта: NLP -> Календарь ---")

    # Создаем мок (заглушку) для клиента Google API
    mock_google_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.return_value = {"id": "test_123", "htmlLink": "https://calendar.google.com/..."}
    mock_google_service.events.return_value.insert.return_value = mock_insert_req

    # Патчим get_calendar_service. new_callable=AsyncMock нужен, т.к. функция async.
    with patch("calendar_connector.get_calendar_service", new_callable=AsyncMock) as mock_get_service:
        # Говорим: когда код сделает await get_calendar_service(), верни mock_google_service
        mock_get_service.return_value = mock_google_service

        # Вызываем твой календарь
        result = await create_event(
            user_id=1,
            token_storage=FakeStorage(),
            event_data=event_data_from_anna
        )

        print("Результат работы календаря:")
        print(result)

        # Честная проверка результата
        if result.get("status") == "success":
            print("\n✅ Успех! Модуль принял данные от NLP, переварил их и 'отправил' в Google.")
        else:
            print("\n❌ Ошибка! Модуль не смог обработать данные.")


if __name__ == "__main__":
    asyncio.run(main())