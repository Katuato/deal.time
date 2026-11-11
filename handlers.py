import asyncio
import logging
import os
import tempfile
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dateparser.search import search_dates
from zoneinfo import ZoneInfo

from config import bot
from stt_module import STTProcessor
from nlp_module import extract_entities_async
from database.models import get_buss_conn, add_buss_conn
log = logging.getLogger("delu.handlers")
rt = Router()
stt = STTProcessor()

@rt.business_connection()
async def check_business_conn(bus_conn: types.BusinessConnection):
    user_id = bus_conn.user.id
    user = await get_buss_conn(user_id)
    if user is not None and (bus_conn.id!=user.business_token or user.status is False or user.status is None) or user is None:
        response = await add_buss_conn(user_id=user_id, business_token=bus_conn.id)
        if response:
            text = "Вы успешно подключили бота к своему аккаунту!"
            user = await get_buss_conn(user_id)
            if user.google_token is None:
                text+="\n\nПожалуйста, свяжите бота с google-календарём"
        else:
            text = "Ошибка при попытке подключения бота."

        await bot.send_message(chat_id=user.user_id, text=text)

# Статусы NLP, при которых пользователю показывается карточка события.
# "no_event" (обычная переписка) и "error" — молча пропускаем.
EVENT_STATUSES = {"ok", "needs_clarification"}


async def transcribe_voice(voice: types.Voice) -> str | None:
    """Скачивает голосовое во временный файл и распознаёт его в фоновом потоке STT.
    Возвращает текст или None, если распознать не удалось."""
    fd, path = tempfile.mkstemp(prefix="voice_", suffix=".ogg")
    os.close(fd)
    try:
        file_info = await bot.get_file(voice.file_id)
        await bot.download(file_info, destination=path)
        return await stt.transcribe_async(audio_path=path)
    except Exception:
        log.exception("STT failed for voice %s", voice.file_unique_id)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@rt.business_message()
async def new_message(msg: types.Message):
    if msg.chat.type != 'private':
        return
    buss_conn = await bot.get_business_connection(msg.business_connection_id)

    if msg.voice:
        text = await transcribe_voice(msg.voice)
    else:
        # стикеры, фото без подписи и т.п. текста не содержат
        text = msg.text or msg.caption
    if not text or not text.strip():
        log.debug("message %s: no text, skipped", msg.message_id)
        return

    answ = await extract_entities_async(text=text, message_time=msg.date)
    status = answ.get("status") if isinstance(answ, dict) else None
    log.info("message %s: nlp status=%s", msg.message_id, status)

    if status in EVENT_STATUSES:
        await new_event(answ, user_id=buss_conn.user.id)
    elif status == "error":
        log.warning("message %s: nlp error=%s", msg.message_id, answ.get("error"))
    # status == "no_event": в сообщении нет договорённости — ничего не отправляем

async def new_event(answ, user_id):
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Добавить в календарь', callback_data='add_event')
    kb.button(text='✍️ Изменить', callback_data='edit_event')
    kb.button(text='🚫 Не добавлять', callback_data='del_event')
    kb.adjust(1)
    
    text = (
        f'Обнаружено новое событие:\n'
        f'...\n'
        'Выберите действие:'
            )
    await bot.send_message(chat_id=user_id, text=text, reply_markup=kb.as_markup())

@rt.callback_query(F.data.in_(['add_event', 'edit_event', 'del_event']))
async def action_event(call: types.CallbackQuery):
    match call.data:
        case "add_event":
            pass
        case "edit_event":
            pass
        case "del_event":
            pass