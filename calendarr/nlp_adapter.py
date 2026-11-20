"""
Адаптер между результатом modules/nlp_module.py и календарным коннектором.

NLP-модуль уже возвращает готовые `starts_at`/`ends_at` (ISO с поясом), `rrule`,
`duration_minutes` и `timezone`, поэтому здесь никакого собственного разбора
дат и повторений нет — только маппинг полей.
"""
import logging

from calendarr.calendar_queue import create_event_or_enqueue

logger = logging.getLogger(__name__)


async def handle_nlp_result(
    nlp_result: dict,
    user_id: int,
    storage,
    user_tz: str = "Europe/Moscow",
    *,
    local_event_id: int | None = None,
) -> dict:
    """Превращает результат NLP в событие календаря и создаёт его (с очередью при сбоях сети)."""
    status = nlp_result.get("status")

    if status == "needs_clarification":
        return {
            "status": "needs_clarification",
            "missing": nlp_result.get("missing", []),
            "question": nlp_result.get("clarify") or nlp_result.get("question"),
        }
    if status != "ok":
        return {"status": "ignored"}

    ev = nlp_result.get("event") or {}
    intent = ev.get("intent")
    if intent in ("move", "cancel"):
        # TODO: перенос/отмена требуют поиска события в БД (events.google_event_id)
        # и вызова update_event_time/delete_event — пока не реализовано.
        return {"status": "unsupported", "intent": intent}
    if intent != "create":
        return {"status": "ignored"}

    if not ev.get("starts_at"):
        return {"status": "needs_clarification", "missing": ["starts_at"],
                "question": nlp_result.get("clarify")}

    # Google API требует email, а NLP отдаёт только имена — кладём их в описание
    desc = "Создано ботом ДЕЛУ.ВРЕМЯ"
    if ev.get("attendees"):
        desc = f"Участники: {', '.join(ev['attendees'])}\n" + desc

    event_data = {
        "summary": ev.get("summary") or "Встреча",
        "start_time": ev["starts_at"],
        "end_time": ev.get("ends_at"),
        "duration_minutes": ev.get("duration_minutes"),
        "timezone": ev.get("timezone") or user_tz,
        "description": desc,
        "attendees": [],
    }
    if ev.get("location"):
        event_data["location"] = ev["location"]
    if ev.get("rrule"):
        event_data["rrule"] = ev["rrule"]

    return await create_event_or_enqueue(
        user_id, storage, event_data, local_event_id=local_event_id
    )
