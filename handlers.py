import asyncio
from aiogram import Router, types, F
from dateparser.search import search_dates
from zoneinfo import ZoneInfo

from config import bot
rt = Router()
@rt.business_connection()
async def check_business_conn(bus_conn: types.BusinessConnection):
    user_id = bus_conn.user.id
    print("Новое подключение:", user_id)

@rt.business_message()
async def new_message(msg: types.Message):
    if msg.chat.type=='private':
        if msg.voice:
            file_id = msg.voice.file_id
            file_info = await bot.get_file(file_id)
            file_destination = f'voices/{file_id}.ogg'
            await bot.download(file_info, destination=file_destination)
        else:
            base_date = msg.date.astimezone(
                ZoneInfo("Europe/Moscow")
            ).replace(tzinfo=None)
            dates = await asyncio.to_thread(
                search_dates,
                msg.text,
                languages = ["ru", "en"],
                settings ={
                    "RELATIVE_BASE": base_date,
                    "DATE_ORDER": "DMY"
                }
            )
            if dates:
                for text, date in dates:
                    clock = f"{date:%d.%m.%Y %H:%M}"
                print(clock)
            print(f"Сообщение от {msg.from_user.full_name}: {msg.text}")