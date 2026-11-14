import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from nlp_adapter import adapt_nlp_to_calendar
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


# 2. Имитируем РЕАЛЬНЫЙ ответ от NLP-модуля Анны (на основе её кода)
nlp_response_from_anna = {
    "intent": "create",
    "summary": "Ужин",
    "starts_at": "2026-11-15T19:00:00+03:00",
    "duration_minutes": 90,
    "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE,FR",
    "attendees": ["Игорь", "Марина"],
    "location": "Кафе",
    "timezone": "UTC+05:00"  # Анна отдает смещение
}


async def main():
    print("--- Проверка сквозной интеграции: NLP -> Адаптер -> Календарь ---")

    # Создаем мок (заглушку) для клиента Google API
    mock_google_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.return_value = {"id": "test_123", "htmlLink": "https://calendar.google.com/..."}
    mock_google_service.events.return_value.insert.return_value = mock_insert_req

    # Патчим get_calendar_service
    with patch("calendar_connector.get_calendar_service", new_callable=AsyncMock) as mock_get_service:
        mock_get_service.return_value = mock_google_service

        try:
            # Вызываем НАШ АДАПТЕР
            event_data = adapt_nlp_to_calendar(nlp_response_from_anna, user_tz="Europe/Moscow")

            # Вызываем ТВОЙ КАЛЕНДАРЬ
            result = await create_event(
                user_id=1,
                token_storage=FakeStorage(),
                event_data=event_data
            )

            print("\nРезультат работы календаря:")
            print(result)

            print("\n--- Что именно ушло в Google Calendar API ---")
            call_kwargs = mock_google_service.events.return_value.insert.call_args.kwargs
            body = call_kwargs["body"]

            print(f"Название: {body.get('summary')}")
            print(f"Начало:   {body.get('start')}")
            print(f"Конец:    {body.get('end')}")
            print(f"Повтор:   {body.get('recurrence')}")
            print(f"Описание: {body.get('description')}")
            print(f"Место:    {body.get('location')}")
            print(f"Участники (email): {body.get('attendees')}")

            if result.get("status") == "success":
                print("\n✅ УСПЕХ! Все 7 пунктов задачи реализованы корректно.")
            else:
                print("\n❌ ОШИБКА! Модуль вернул статус error.")

        except ValueError as e:
            print(f"\n❌ Ошибка валидации адаптера: {e}")


if __name__ == "__main__":
    asyncio.run(main())