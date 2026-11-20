import os
import logging
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
load_dotenv()
token = os.getenv('TOKEN')
db_logpass = os.getenv('db_logpass')
db_ipport = os.getenv('db_ipport')
secret_key = os.getenv('secret_key')
bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
logger = logging.getLogger(__name__)
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
GOOGLE_REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI')