import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from nlp_adapter import handle_nlp_result


# 1. Имитируем базу данных
class FakeStorage:
    async def get_user_token(self, user_id):
        return {
            "token": "fake", "refresh_token": "fake", "client_id": "fake",
            "client_secret": "fake", "token_uri": "fake"
        }

    async def save_user_token(self, user_id, token_data):
        pass


# 2. Имитируем РЕАЛЬНЫЙ ответ от NLP-модуля Анны (в том виде, в котором она его отдает)
nlp_response_from_anna = {
    "status": "ok",
    "event": {
        "intent": "create",
        "summary": "Встреча с командой",
        "date": "завтра",
        "time": "15:00",
        "recurrence": "каждую неделю пн",
        "attendees": ["Павел", "Аня"]
    },
    "missing": [],
    "error": None
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

        # Вызываем НАШ АДАПТЕР (он внутри вызовет твой create_event)
        result = await handle_nlp_result(
            nlp_result=nlp_response_from_anna,
            user_id=1,
            storage=FakeStorage()
        )

        print("\nРезультат работы календаря:")
        print(result)

        # Проверяем, что адаптер правильно преобразовал данные перед отправкой в Google
        print("\n--- Что именно ушло в Google Calendar API ---")
        call_kwargs = mock_google_service.events.return_value.insert.call_args.kwargs
        body = call_kwargs["body"]

        # Красивый вывод для проверки
        print(f"Название: {body.get('summary')}")
        print(f"Начало:   {body.get('start')}")
        print(f"Повтор:   {body.get('recurrence')}")
        print(f"Описание: {body.get('description')}")
        print(f"Участники (email): {body.get('attendees')}")

        if result.get("status") == "success":
            print("\n✅ УСПЕХ! Адаптер переварил ответ NLP и корректно вызвал твой модуль календаря.")
        else:
            print("\n❌ ОШИБКА! Что-то пошло не так.")


if __name__ == "__main__":
    asyncio.run(main())