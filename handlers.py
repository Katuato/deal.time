import asyncio
import os
from aiogram import Router, types, F
from dateparser.search import search_dates
from zoneinfo import ZoneInfo

from config import bot
from stt_module import STTProcessor
from nlp_module import extract_entities_async
from database.models import get_buss_conn, add_buss_conn
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

@rt.business_message()
async def new_message(msg: types.Message):
    if msg.chat.type=='private':
        if msg.voice:
            file_id = msg.voice.file_id
            file_info = await bot.get_file(file_id)
            file_destination = f'voices/{file_id}.ogg'
            await bot.download(file_info, destination=file_destination)
            text = stt.transcribe(audio_path=file_destination)
            os.remove(file_destination)
        else:
            text = msg.text
        print(text)
        answ = await extract_entities_async(text=text)
        print(answ)