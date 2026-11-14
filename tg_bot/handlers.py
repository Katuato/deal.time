import asyncio
import logging
import os
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from dateparser.search import search_dates
from zoneinfo import ZoneInfo
from datetime import datetime

from config import bot, logger
from modules.stt_module import STTProcessor
from modules.nlp_module import process_dialogue_message, DialogueState, DEFAULT_TZ

dialogue_states: dict[tuple[int, int], DialogueState] = {}
from database.models import get_buss_conn, add_buss_conn, get_user
from calendarr.oauth import build_auth_url
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
        state = dialogue_states.setdefault((owner_id, msg.chat.id), DialogueState())
        try:
            # модель работает синхронно и долго, поэтому выполняем в отдельном потоке,
            # чтобы не блокировать event loop бота
            answ = await asyncio.to_thread(
                process_dialogue_message, text=text, state=state, message_time=msg.date, tz=tz,
            )
        except ValueError as e:
            logger.warning("NLP отклонил сообщение: %s", e)
            return
        logger.info("NLP status=%s", answ.get("status"))
        if answ.get("status") in ("ok", "needs_clarification"):
            await new_event(answ, user_id=owner_id)

async def new_event(answ, user_id, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Добавить в календарь', callback_data='add_event')
    kb.button(text='✍️ Изменить', callback_data='edit_event')
    kb.button(text='🚫 Не добавлять', callback_data='del_event')
    kb.adjust(1)
    
    ev = answ.get("event") or {}
    lines = ['Обнаружено новое событие:']
    if ev.get("summary"):
        lines.append(f'📌 {ev["summary"]}')
    if ev.get("starts_at"):
        start = datetime.fromisoformat(ev.get("starts_at")).strftime("%d.%m.%Y в %H:%M")
        lines.append(f'🕒 {start}')
    if ev.get("location"):
        lines.append(f'📍 {ev["location"]}')
    if answ.get("clarify"):
        lines.append(f'❓ Уточните: {answ["clarify"]}')
    lines.append('Выберите действие:')
    text = '\n'.join(lines)
    state.set_data(event_text=text, event=ev)
    await bot.send_message(chat_id=user_id, text=text, reply_markup=kb.as_markup())

@rt.callback_query(F.data.in_(['add_event', 'edit_event', 'del_event']))
async def action_event(call: types.CallbackQuery, state: FSMContext):
    user = await get_user(user_id=call.from_user.id)
    conn_info = await get_buss_conn(user)
    try:
        state_data = await state.get_data()
        event_text = state_data.get("event_text")
        event = state_data.get("event")
    except Exception as e:
        logger.warning("Ошибка при получении state в изменении event: %s", e)
        await call.answer('Не удалось получить информацию о событии.')
    match call.data:
        case "add_event":
            match conn_info.google_token:
                case None:
                    await call.answer("Пожалуйста, подключите google календарь, отправив команду /start", show_alert=True)
                case _:
                    pass
        case "edit_event":
            kb = InlineKeyboardBuilder()
            kb.button("📌 Название события", callback_data="edit_name")
            kb.button("🕒 Время", callback_data="edit_time")
            kb.button("📍 Место", callback_data="edit_place")
            kb.adjust(1)
            await call.message.edit_text("Что вы хотите изменить?", reply_markup=kb.as_markup())
        case "del_event":
            await call.message.edit_text(f"{event_text}\n\n❌ Отменено")