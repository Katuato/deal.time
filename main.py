import logging
import asyncio
import os
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Dispatcher, Bot
from dotenv import load_dotenv
from handlers import rt
logging.basicConfig(level=logging.INFO)
load_dotenv()
token = os.getenv('TOKEN')
bot = Bot(token=token)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.include_router(rt)
async def main():
    await dp.start_polling(bot)
if __name__=='__main__':
    asyncio.run(main())
