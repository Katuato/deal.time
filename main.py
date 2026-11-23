import asyncio
import os

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from calendarr.calendar_queue import run_worker
from calendarr.oauth_server import start_oauth_server
from calendarr.storage import DBTokenStorage
from config import bot
from database.db import check_database, engine, get_token_cipher
from logging_setup import LoggingContextMiddleware, setup_logging
from tg_bot.handlers import initialize_stt, rt
from tg_bot.user_end import us

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.update.outer_middleware(LoggingContextMiddleware())
dp.include_router(rt)
dp.include_router(us)


async def main():
    setup_logging()
    try:
        get_token_cipher()
        await check_database()
        await initialize_stt()
        runner = await start_oauth_server(
            os.getenv("OAUTH_HOST", "0.0.0.0"), int(os.getenv("OAUTH_PORT", "8080"))
        )
        worker_task = asyncio.create_task(run_worker(DBTokenStorage()))
        try:
            await dp.start_polling(bot, close_bot_session=False)
        finally:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            await runner.cleanup()
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
