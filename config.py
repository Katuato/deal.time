import os
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
load_dotenv()
token = os.getenv('TOKEN')
bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))