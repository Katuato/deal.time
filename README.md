# Модуль календаря «ДЕЛУ.ВРЕМЯ»

## Публичный API

```python
from app.calendar.calendar_connector import (
    create_event, update_event_time, delete_event, CalendarAuthError,
)
from app.calendar.storage import DBTokenStorage

storage = DBTokenStorage()

result = await create_event(user_id, storage, event_data)
# {"status": "success", "event_id": str, "html_link": str|None, "duplicate": bool}
# {"status": "error", "error_type": "auth_error"|"api_error"|"api_unavailable"|"validation_error",
#  "message": str, "need_queue": bool}

result = await update_event_time(user_id, storage, event_id, new_start, new_end, timezone="Europe/Moscow")
result = await delete_event(user_id, storage, event_id)