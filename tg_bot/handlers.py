import asyncio
import html
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from aiogram import F, Router, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from calendarr.calendar_queue import confirm_event_and_enqueue
from calendarr.nlp_adapter import build_event_data
from calendarr.oauth import build_auth_url
from config import bot, logger
from database.models import (
    add_buss_conn, add_event, get_buss_conn, get_event, get_user, has_active_consent, update_event,
)
from modules.nlp_module import (process_dialogue_message, DialogueState, DEFAULT_TZ, resolve_date,
                                 merge_clarification, get_zone, Event as NlpEvent, fill_datetimes,
                                 to_event_record)

import tg_bot.keyboards as keyb

rt = Router()
dialogue_states: dict[tuple[int, int], DialogueState] = {}
_stt = None
_stt_lock = asyncio.Lock()


async def initialize_stt():
    global _stt
    async with _stt_lock:
        if _stt is None:
            from modules.stt_module import STTProcessor

            _stt = await asyncio.to_thread(STTProcessor)
        return _stt


async def _transcribe_voice(voice: types.Voice) -> str | None:
    try:
        processor = await initialize_stt()
        async with _stt_lock:
            with BytesIO() as audio:
                await bot.download(voice, destination=audio)
                return await asyncio.to_thread(processor.transcribe, audio.getvalue())
    except Exception as error:
        logger.warning("Не удалось обработать голосовое сообщение (%s)", type(error).__name__)
        return None


@rt.business_connection()
async def check_business_conn(bus_conn: types.BusinessConnection):
    user_id = bus_conn.user.id
    if not await has_active_consent(user_id):
        return
    connection = await get_buss_conn(user_id)
    already_linked = connection is not None and connection.business_token == bus_conn.id
    if not bus_conn.is_enabled:
        if already_linked:
            await add_buss_conn(user_id=user_id, business_token=bus_conn.id, status=False)
        return
    if already_linked and connection.status:
        return
    if not await add_buss_conn(user_id=user_id, business_token=bus_conn.id, status=True):
        await bot.send_message(chat_id=user_id, text="Не удалось подключить бота.")
        return
    markup = None
    text = "Вы успешно подключили бота к своему аккаунту!"
    if connection is None or connection.google_token is None:
        markup = keyb.google_conn_kb()
        text += "\n\nПожалуйста, подключите Google Календарь."
    await bot.send_message(chat_id=user_id, text=text, reply_markup=markup)


@rt.callback_query(F.data=="google_connect")
async def google_connect(call: types.CallbackQuery):
    if not await has_active_consent(call.from_user.id):
        await call.answer("Сначала подтвердите согласие через /start.", show_alert=True)
        return
    try:
        url = build_auth_url(user_id=call.from_user.id)
    except Exception as error:
        logger.warning("Не удалось подготовить ссылку Google OAuth (%s)", type(error).__name__)
        await call.message.edit_text(text="Произошла ошибка при генерации ссылки.")
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Ссылка для подключения", url=url)
    await call.message.edit_text(text="Пожалуйста, перейдите по ссылке ниже, чтобы подключить календарь!", reply_markup=kb.as_markup())
    await call.answer()

@rt.business_message()
async def new_message(msg: types.Message):
    if msg.chat.type != "private" or not msg.business_connection_id or not (msg.voice or msg.text):
        return
    try:
        buss_conn = await bot.get_business_connection(msg.business_connection_id)
    except Exception as error:
        logger.warning("Не удалось проверить подключение Telegram Business (%s)", type(error).__name__)
        return
    owner_id = buss_conn.user.id
    dialogue_key = (owner_id, msg.chat.id)
    if (buss_conn.id != msg.business_connection_id
            or not buss_conn.is_enabled
            or not await has_active_consent(owner_id)):
        dialogue_states.pop(dialogue_key, None)
        return

    text = await _transcribe_voice(msg.voice) if msg.voice else msg.text
    if not text or not text.strip():
        return
    user = await get_user(owner_id)
    tz = user.timezone if user is not None and user.timezone else DEFAULT_TZ
    dialogue_state = dialogue_states.setdefault(dialogue_key, DialogueState())
    try:
        answ = await asyncio.to_thread(
            process_dialogue_message, text=text, state=dialogue_state, message_time=msg.date, tz=tz,
        )
    except ValueError:
        logger.warning("NLP отклонил сообщение")
        return
    status = answ.get("status")
    intent = (answ.get("event") or {}).get("intent")
    logger.info("NLP status=%s intent=%s", status, intent)

    # После обработки черновика не показываем его снова на каждое сообщение.
    if intent in ("move", "cancel"):
        dialogue_state.reset()
        return
    if status == "needs_clarification" and intent == "create":
        guessed = _guess_ambiguous_time(answ, msg.date, tz)
        if guessed is not None:
            answ, status = guessed, "ok"
    if status != "ok" or intent != "create":
        return
    dialogue_state.reset()
    await new_event(answ, user_id=owner_id, chat_id=msg.chat.id, user_tz=tz)

def _guess_ambiguous_time(answ: dict, message_time: datetime, tz: str) -> dict | None:
    """Если не хватает только однозначного времени, а дата и вариант времени есть, — подставляет
    вариант, который выбрал NLP (для «в 5» это 17:00), и возвращает ответ с заполненным starts_at.
    В остальных случаях (нет даты, нет времени, время уже прошло) — None."""
    if set(answ.get("missing") or []) - {"time"}:
        return None
    raw = answ.get("event") or {}
    try:
        ev = NlpEvent.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None
    if not (ev.time_ambiguous or ev.time_options):
        return None
    if ev.time_options and not ev.time:
        ev.time = ev.time_options[0]
    if not ev.time:
        return None
    ev.time_ambiguous, ev.time_options = False, []
    fill_datetimes(ev, message_time, tz)
    if not ev.starts_at:
        return None
    return {**answ, "status": "ok", "event": {**raw, "starts_at": ev.starts_at, "ends_at": ev.ends_at}}


class EditEvent(StatesGroup):
    waiting_value = State()


EDIT_PROMPTS = {
    "title": "Отправьте новое название события.",
    "time": "Отправьте новую дату и время, например: «завтра в 15:00» или «25.11 18:30».",
    "location": "Отправьте новое место. Чтобы убрать место, отправьте «-».",
}


def _to_naive(value: str | None) -> datetime | None:
    """ISO-строка с поясом -> местное время без пояса (колонки в БД без часового пояса)."""
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=None)


def _event_card(ev) -> list[str]:
    """Строки карточки события по записи из БД."""
    lines = []
    if ev.title:
        lines.append(f'📌 {html.escape(ev.title)}')
    if ev.starts_at:
        lines.append(f'🕒 {ev.starts_at.strftime("%d.%m.%Y в %H:%M")}')
    if ev.location:
        lines.append(f'📍 {html.escape(ev.location)}')
    return lines


def _card_text(ev) -> str:
    status = {
        "waiting": "Выберите действие:",
        "queued": "⏳ Событие в очереди на добавление в Google Календарь.",
        "confirmed": "✅ Событие добавлено в Google Календарь.",
        "failed": "⚠️ Не удалось добавить событие в Google Календарь.",
        "declined": "❌ Событие не добавлено.",
    }
    return '\n'.join(["Событие:", *_event_card(ev), status.get(ev.status, "Статус недоступен.")])


def _event_kb(event_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Добавить в календарь', callback_data=f'ev:add:{event_id}')
    kb.button(text='✍️ Изменить', callback_data=f'ev:edit:{event_id}')
    kb.button(text='🚫 Не добавлять', callback_data=f'ev:del:{event_id}')
    kb.adjust(1)
    return kb.as_markup()


def _status_kb(event_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="Проверить статус", callback_data=f"ev:show:{event_id}")
    return kb.as_markup()


def _parse_new_time(text: str, ev) -> datetime | None:
    """Разбирает «завтра в 15:00», «в пятницу в 10», «25.11 18:30», «в 7 вечера» через NLP-модуль
    (merge_clarification). Без даты день остаётся прежним. Возвращает время без пояса (в поясе события)."""
    tz = ev.timezone or DEFAULT_TZ
    now = datetime.now(get_zone(tz) or ZoneInfo(DEFAULT_TZ))
    draft = merge_clarification(NlpEvent(intent="create"), text, now, tz)

    # «в 5» NLP помечает неоднозначным (05:00/17:00) — берём его основной вариант, как при создании
    hhmm = draft.time or (draft.time_options[0] if draft.time_options else None)
    if hhmm is None:
        if not (draft.date and ev.starts_at):
            return None
        hhmm = ev.starts_at.strftime("%H:%M")  # «послезавтра» — меняем только день, время прежнее

    if draft.date:
        day = resolve_date(draft.date, now)
        if day is None:
            return None
    else:
        day = ev.starts_at or now
    return datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[3:]))


async def new_event(answ, user_id, chat_id, user_tz):
    rec = to_event_record(answ, user_id=user_id, chat_id=chat_id, user_tz=user_tz)
    if rec is None:  # не create или статус не ok — сюда не должно попадать
        logger.warning("new_event: NLP-результат не подходит для создания события")
        return
    # колонки БД: время без пояса, длительность — строка с минутами, статус до подтверждения — waiting
    event_id = await add_event(
        user_id=user_id, chat_id=chat_id, title=rec["title"],
        starts_at=_to_naive(rec["starts_at"]), ends_at=_to_naive(rec["ends_at"]),
        duration=str(int(rec["duration"].total_seconds() // 60)) if rec["duration"] else None,
        timezone=rec["timezone"], location=rec["location"],
        participants=rec["participants"] or None,
        repeating=rec["rrule"],
        status="waiting",
    )
    if event_id is None:
        await bot.send_message(chat_id=user_id, text="Не удалось сохранить новое событие. Попробуйте позже.")
        return
    saved = await get_event(event_id)
    if saved is None:
        await bot.send_message(chat_id=user_id, text="Не удалось показать карточку события.")
        return
    lines = ['Обнаружено новое событие:', *_event_card(saved)]
    if answ.get("clarify"):
        lines.append(f'❓ {html.escape(answ["clarify"])}')
    lines.append('Выберите действие:')
    await bot.send_message(chat_id=user_id, text='\n'.join(lines), reply_markup=_event_kb(event_id))


def _calendar_event_data(ev):
    return build_event_data(
        summary=ev.title,
        start_time=ev.starts_at.isoformat(),
        end_time=ev.ends_at.isoformat() if ev.ends_at else None,
        duration_minutes=int(ev.duration) if ev.duration and ev.duration.isdigit() else None,
        timezone=ev.timezone or DEFAULT_TZ,
        attendees=[p.get("name") for p in ev.participants or [] if p.get("name")],
        location=ev.location,
        rrule=ev.repeating,
    )


async def _add_to_calendar(call: types.CallbackQuery, ev):
    if ev.status != "waiting":
        await call.answer("Событие уже обработано.", show_alert=True)
        return
    if not ev.starts_at:
        await call.answer("У события не указано время начала — уточните его.", show_alert=True)
        return
    if not await has_active_consent(ev.user_id):
        await call.answer("Сначала подтвердите согласие через /start.", show_alert=True)
        return
    conn_info = await get_buss_conn(ev.user_id)
    if conn_info is None or conn_info.google_token is None:
        await call.message.answer("Сначала подключите Google Календарь:", reply_markup=keyb.google_conn_kb())
        await call.answer()
        return
    try:
        operation_id = await confirm_event_and_enqueue(ev.user_id, ev.id, _calendar_event_data)
    except Exception as error:
        logger.warning("Не удалось поставить событие в очередь (%s)", type(error).__name__)
        await call.answer("Не удалось сохранить событие. Попробуйте ещё раз.", show_alert=True)
        return
    if operation_id is None:
        await call.answer("Событие уже обработано.", show_alert=True)
        return

    await call.answer("Событие сохранено.")
    saved = await get_event(ev.id)
    text = _card_text(saved) if saved is not None else "⏳ Событие поставлено в очередь."
    await call.message.edit_text(text, reply_markup=_status_kb(ev.id))


@rt.callback_query(F.data.startswith("ev:"))
async def action_event(call: types.CallbackQuery, state: FSMContext):
    try:
        _, action, raw_id = call.data.split(":")
        event_id = int(raw_id)
    except ValueError:
        await call.answer("Некорректная кнопка.", show_alert=True)
        return
    ev = await get_event(event_id)
    if ev is None or ev.user_id != call.from_user.id:
        await call.answer("Событие не найдено.", show_alert=True)
        return

    match action:
        case "add":
            await _add_to_calendar(call, ev)
        case "edit":
            if ev.status != "waiting":
                await call.answer("Событие уже обработано — изменить его нельзя.", show_alert=True)
                return
            kb = InlineKeyboardBuilder()
            kb.button(text="📌 Название события", callback_data=f"ev:edit_title:{event_id}")
            kb.button(text="🕒 Время", callback_data=f"ev:edit_time:{event_id}")
            kb.button(text="📍 Место", callback_data=f"ev:edit_location:{event_id}")
            kb.button(text="↩️ Назад", callback_data=f"ev:show:{event_id}")
            kb.adjust(1)
            await call.message.edit_text("Что вы хотите изменить?", reply_markup=kb.as_markup())
            await call.answer()
        case "edit_title" | "edit_time" | "edit_location":
            if ev.status != "waiting":
                await call.answer("Событие уже обработано — изменить его нельзя.", show_alert=True)
                return
            field = action.removeprefix("edit_")
            await state.set_state(EditEvent.waiting_value)
            await state.update_data(event_id=event_id, field=field, cleanup=[call.message.message_id])
            kb = InlineKeyboardBuilder()
            kb.button(text="↩️ Отмена", callback_data=f"ev:show:{event_id}")
            await call.message.edit_text(EDIT_PROMPTS[field], reply_markup=kb.as_markup())
            await call.answer()
        case "show":
            extra = [m for m in (await state.get_data()).get("cleanup", []) if m != call.message.message_id]
            await state.clear()
            await _delete_messages(call.message.chat.id, extra)
            # Повторное нажатие для статуса «в очереди» не меняет карточку.
            if ev.status != "queued":
                markup = _event_kb(event_id) if ev.status == "waiting" else None
                await call.message.edit_text(_card_text(ev), reply_markup=markup)
            else:
                await call.answer("Событие ещё в очереди.")
                return
            await call.answer()
        case "del":
            if not await update_event(event_id, waiting_user_id=ev.user_id, status="declined"):
                await call.answer("Событие больше нельзя отклонить.", show_alert=True)
                return
            await call.message.edit_text('\n'.join(_event_card(ev)) + "\n\n❌ Не добавлено")
            await call.answer()
        case _:
            await call.answer("Неизвестное действие.", show_alert=True)


@rt.callback_query(F.data.in_(['add_event', 'edit_event', 'del_event']))
async def old_event_buttons(call: types.CallbackQuery):
    """Кнопки из сообщений, отправленных до обновления бота, — в них нет id события."""
    await call.answer("Эта кнопка устарела. Отправьте сообщение о событии ещё раз.", show_alert=True)


async def _delete_messages(chat_id: int, message_ids: list[int]) -> None:
    """Удаляет служебные сообщения бота. Если удалить нельзя (старше 48 часов) — убирает у них кнопки."""
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            except Exception:
                pass


async def _reply_and_remember(msg: types.Message, state: FSMContext, text: str) -> None:
    """Ответ с ошибкой ввода; запоминаем его, чтобы удалить после успешной правки."""
    sent = await msg.answer(text)
    data = await state.get_data()
    await state.update_data(cleanup=[*data.get("cleanup", []), sent.message_id])


@rt.message(EditEvent.waiting_value, F.text)
async def edit_event_value(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    event_id, field = data.get("event_id"), data.get("field")
    ev = await get_event(event_id) if event_id else None
    if ev is None or ev.user_id != msg.from_user.id or ev.status != "waiting":
        await state.clear()
        await msg.answer("Это событие больше нельзя изменить.")
        return

    text = msg.text.strip()
    match field:
        case "title":
            if not text:
                await _reply_and_remember(msg, state, "Название не может быть пустым. Отправьте новое название.")
                return
            changes = {"title": text[:200]}
        case "location":
            changes = {"location": None if text == "-" else text[:200]}
        case "time":
            new_start = _parse_new_time(text, ev)
            if new_start is None:
                await _reply_and_remember(msg, state, "Не понял время. " + EDIT_PROMPTS["time"])
                return
            changes = {"starts_at": new_start}
            if ev.starts_at and ev.ends_at:
                changes["ends_at"] = new_start + (ev.ends_at - ev.starts_at)
        case _:
            await state.clear()
            return

    if not await update_event(event_id, waiting_user_id=ev.user_id, **changes):
        await _reply_and_remember(msg, state, "Не удалось сохранить изменения. Попробуйте ещё раз.")
        return
    await state.clear()
    await _delete_messages(msg.chat.id, data.get("cleanup", []))
    ev = await get_event(event_id)
    if ev is not None:
        markup = _event_kb(event_id) if ev.status == "waiting" else None
        await msg.answer(_card_text(ev), reply_markup=markup)
