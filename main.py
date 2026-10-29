import logging
import asyncio
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Dispatcher
from handlers import rt

from config import bot
from database.db import async_main
logging.basicConfig(level=logging.INFO)


storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.include_router(rt)
async def main():
    await async_main()
    await dp.start_polling(bot)
if __name__=='__main__':
    asyncio.run(main())
