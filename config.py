import os
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
load_dotenv()
token = os.getenv('TOKEN')
db_logpass = os.getenv('db_logpass')
db_ipport = os.getenv('db_ipport')
secret_key = os.getenv('secret_key')
bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
