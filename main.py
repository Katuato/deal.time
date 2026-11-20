import logging
import os
import asyncio
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Dispatcher
from tg_bot.handlers import rt
from tg_bot.user_end import us


from config import bot
from database.db import async_main
from calendarr.oauth_server import start_oauth_server
from calendarr.calendar_queue import run_worker
from calendarr.storage import DBTokenStorage
from logging_setup import setup_logging, LoggingContextMiddleware
setup_logging()
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.update.outer_middleware(LoggingContextMiddleware()) 
dp.include_router(rt)
dp.include_router(us)

async def main():
    print("Начинаю подключение к базе", flush=True)
    await async_main()
    print("База подключена", flush=True)
    runner = await start_oauth_server(
        os.getenv("OAUTH_HOST", "0.0.0.0"), int(os.getenv("OAUTH_PORT", "8080"))
    )
    worker_task = asyncio.create_task(run_worker(DBTokenStorage()))
    try:
        print("пулинг", flush=True)
        await dp.start_polling(bot)
        print("пулинг +++", flush=True)
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await runner.cleanup()
if __name__=='__main__':
    asyncio.run(main())
