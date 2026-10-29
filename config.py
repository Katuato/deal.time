import os
from aiogram import Bot
from dotenv import load_dotenv
load_dotenv()
token = os.getenv('TOKEN')
bot = Bot(token=token)