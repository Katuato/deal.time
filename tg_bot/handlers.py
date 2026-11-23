import asyncio
import os
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from zoneinfo import ZoneInfo
import html
from datetime import datetime

from config import bot, logger
from modules.stt_module import STTProcessor
from modules.nlp_module import (process_dialogue_message, DialogueState, DEFAULT_TZ, resolve_date,
                                 merge_clarification, get_zone, Event as NlpEvent, fill_datetimes,
                                 to_event_record)

dialogue_states: dict[tuple[int, int], DialogueState] = {}
from database.models import get_buss_conn, add_buss_conn, get_user, add_event, update_event, get_event
from calendarr.oauth import build_auth_url
from calendarr.calendar_queue import create_event_or_enqueue
from calendarr.nlp_adapter import build_event_data
from calendarr.storage import DBTokenStorage
import tg_bot.keyboards as keyb

rt = Router()
stt = STTProcessor()

@rt.business_connection()
async def check_business_conn(bus_conn: types.BusinessConnection):
    '''Проверка бизнес подключения'''
    user_id = bus_conn.user.id
    user = await get_buss_conn(user_id)
    if user is not None and (bus_conn.id!=user.business_token or user.status is False or user.status is None) or user is None:
        response = await add_buss_conn(user_id=user_id, business_token=bus_conn.id)
        markup = None
        if response:
            text = "Вы успешно подключили бота к своему аккаунту!"
            user = await get_buss_conn(user_id)
            if user.google_token is None:
                markup = keyb.google_conn_kb()
                text+="\n\nПожалуйста, свяжите бота с google-календарём"
        else:
            text = "Ошибка при попытке подключения бота."
        await bot.send_message(chat_id=user.user_id, text=text, reply_markup=markup)

@rt.callback_query(F.data=="google_connect")
async def google_connect(call: types.CallbackQuery):
    try:
        url = build_auth_url(user_id=call.from_user.id)
    except Exception as e:
        logger.warning("Произошла ошибка при генерации ссылки: %s", e)
        await call.message.edit_text(text="Произошла ошибка при генерации ссылки.")
        return False
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Ссылка для подключения", url=url)
    await call.message.edit_text(text="Пожалуйста, перейдите по ссылке ниже, чтобы подключить календарь!", reply_markup=kb.as_markup())

@rt.business_message()
async def new_message(msg: types.Message):
    if msg.chat.type=='private':
        buss_conn = await bot.get_business_connection(msg.business_connection_id)
        user = await get_user(buss_conn.user.id)
        if msg.voice:
            file_id = msg.voice.file_id
            file_info = await bot.get_file(file_id)
            file_destination = f'voices/{file_id}.ogg'
            await bot.download(file_info, destination=file_destination)
            text = stt.transcribe(audio_path=file_destination)
            os.remove(file_destination)
        else:
            text = msg.text
        if not text or not text.strip():
            return  # стикеры, фото без подписи и т.п.
        owner_id = buss_conn.user.id
        tz = user.timezone if user is not None and user.timezone else DEFAULT_TZ
        dialogue_state = dialogue_states.setdefault((owner_id, msg.chat.id), DialogueState())
        try:
            answ = await asyncio.to_thread(
                process_dialogue_message, text=text, state=dialogue_state, message_time=msg.date, tz=tz,
            )
        except ValueError as e:
            logger.warning("NLP отклонил сообщение: %s", e)
            return
        status = answ.get("status")
        intent = (answ.get("event") or {}).get("intent")
        logger.info("NLP status=%s intent=%s", status, intent)

        # Черновик события живёт в dialogue_state 30 минут, и пока он там лежит, NLP возвращает
        # его на КАЖДОЕ следующее сообщение — даже на «как дела». Поэтому черновик сбрасываем,
        # как только он обработан, иначе уведомление приходило бы на любое сообщение
        if intent in ("move", "cancel"):
            # перенос/отмена пока не поддерживаются (нужен выбор события)
            dialogue_state.reset()
            return
        if status == "needs_clarification" and intent == "create":
            # «часов в 5» — время неоднозначное (05:00 или 17:00). Показываем карточку с наиболее
            # вероятным вариантом и вопросом; время можно поправить кнопкой «Изменить».
            guessed = _guess_ambiguous_time(answ, msg.date, tz)
            if guessed is not None:
                answ, status = guessed, "ok"
        if status != "ok" or intent != "create":
            # no_event — в сообщении нет события; needs_clarification без даты или времени
            # (например, «встретимся завтра»): ждём, пока они появятся в переписке, и молчим
            return
        dialogue_state.reset()
        await new_event(answ, user_id=owner_id, chat_id=msg.chat.id, msg_from=msg.from_user.id,
                        msg_to=msg.receiver_user.id if msg.receiver_user else None, user_tz=tz)

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


# Правило повторения в таблице events пока не хранится (колонка repeating не сделана),
# поэтому держим его в памяти до нажатия «Добавить». После перезапуска бота повтор теряется.
pending_rrules: dict[int, str] = {}


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
    return '\n'.join(['Событие:', *_event_card(ev), 'Выберите действие:'])


def _event_kb(event_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Добавить в календарь', callback_data=f'ev:add:{event_id}')
    kb.button(text='✍️ Изменить', callback_data=f'ev:edit:{event_id}')
    kb.button(text='🚫 Не добавлять', callback_data=f'ev:del:{event_id}')
    kb.adjust(1)
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


async def new_event(answ, user_id, chat_id, user_tz, msg_from=None, msg_to=None):
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
        status="waiting",
    )
    if event_id is None:
        await bot.send_message(chat_id=user_id, text="Не удалось сохранить новое событие. Попробуйте позже.")
        return
    if rec["rrule"]:
        pending_rrules[event_id] = rec["rrule"]

    saved = await get_event(event_id)
    lines = ['Обнаружено новое событие:', *_event_card(saved)]
    if answ.get("clarify"):
        lines.append(f'❓ {html.escape(answ["clarify"])}')
    lines.append('Выберите действие:')
    await bot.send_message(chat_id=user_id, text='\n'.join(lines), reply_markup=_event_kb(event_id))


async def _add_to_calendar(call: types.CallbackQuery, ev):
    if ev.status != "waiting":
        await call.answer("Событие уже обработано.", show_alert=True)
        return
    if not ev.starts_at:
        await call.answer("У события не указано время начала — уточните его.", show_alert=True)
        return
    conn_info = await get_buss_conn(ev.user_id)
    if conn_info is None or conn_info.google_token is None:
        await call.message.answer("Сначала подключите Google Календарь:", reply_markup=keyb.google_conn_kb())
        await call.answer()
        return

    event_data = build_event_data(
        summary=ev.title,
        start_time=ev.starts_at.isoformat(),
        end_time=ev.ends_at.isoformat() if ev.ends_at else None,
        duration_minutes=int(ev.duration) if ev.duration and ev.duration.isdigit() else None,
        timezone=ev.timezone or DEFAULT_TZ,
        attendees=[p.get("name") for p in ev.participants or [] if p.get("name")],
        location=ev.location,
        rrule=pending_rrules.get(ev.id),
    )

    await call.answer("Добавляю в календарь…")
    result = await create_event_or_enqueue(ev.user_id, DBTokenStorage(), event_data, local_event_id=ev.id)
    card = '\n'.join(_event_card(ev))

    if result.get("status") == "success":
        pending_rrules.pop(ev.id, None)
        await update_event(ev.id, status="confirmed", confirmed_at=datetime.now().replace(microsecond=0))
        link = result.get("html_link")
        done = f'<a href="{html.escape(link)}">✅ Добавлено в календарь</a>' if link else "✅ Добавлено в календарь"
        await call.message.edit_text(f"{card}\n\n{done}")
    elif result.get("queued"):
        pending_rrules.pop(ev.id, None)
        await update_event(ev.id, status="queued")
        await call.message.edit_text(f"{card}\n\n⏳ Google Календарь временно недоступен — событие добавится автоматически.")
    elif result.get("error_type") == "auth_error":
        await call.message.answer("Доступ к Google Календарю потерян. Подключите его заново:",
                                  reply_markup=keyb.google_conn_kb())
    else:
        logger.warning("Не удалось создать событие id=%s: %s", ev.id, result)
        await call.message.answer(f"Не удалось добавить событие: {html.escape(result.get('message') or 'неизвестная ошибка')}")


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
            await call.message.edit_text(_card_text(ev),
                                         reply_markup=_event_kb(event_id) if ev.status == "waiting" else None)
            await call.answer()
        case "del":
            await update_event(event_id, status="declined")
            pending_rrules.pop(event_id, None)
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

    if not await update_event(event_id, **changes):
        await _reply_and_remember(msg, state, "Не удалось сохранить изменения. Попробуйте ещё раз.")
        return
    await state.clear()
    await _delete_messages(msg.chat.id, data.get("cleanup", []))
    ev = await get_event(event_id)
    await msg.answer(_card_text(ev), reply_markup=_event_kb(event_id))