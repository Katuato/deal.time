import logging
import asyncio
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Dispatcher
from tg_bot.handlers import rt
from tg_bot.user_end import us


from config import bot
from database.db import async_main
from logging_setup import setup_logging, LoggingContextMiddleware
setup_logging()
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.update.outer_middleware(LoggingContextMiddleware()) 
dp.include_router(rt)
dp.include_router(us)

async def main():
    await async_main()
    await dp.start_polling(bot)
if __name__=='__main__':
    asyncio.run(main())
