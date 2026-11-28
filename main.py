import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import uvicorn
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from calendarr.calendar_queue import run_worker
from calendarr.miniapp_api import api
from calendarr.oauth_server import create_app
from calendarr.storage import DBTokenStorage
from config import bot
from database.db import check_database, engine, get_token_cipher
from logging_setup import LoggingContextMiddleware, setup_logging
from tg_bot.commands import cm, setup_bot_menu
from tg_bot.handlers import initialize_stt, notify_queue_result, rt
from tg_bot.settings import router as settings_router
from tg_bot.user_end import us

logger = logging.getLogger(__name__)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.update.outer_middleware(LoggingContextMiddleware())
dp.include_router(cm)  # /help и /history первыми: работают в любом состоянии диалога
dp.include_router(settings_router)
dp.include_router(rt)
dp.include_router(us)


async def main():
    setup_logging()
    background_failed = False

    def background_stopped(task: asyncio.Task[None]) -> None:
        nonlocal background_failed
        if server.should_exit:
            return
        error = None if task.cancelled() else task.exception()
        reason = type(error).__name__ if error is not None else "задача завершилась"
        logger.error("Остановился %s (%s)", task.get_name(), reason)
        background_failed = True
        server.should_exit = True

    @asynccontextmanager
    async def lifespan(_app):
        async with AsyncExitStack() as resources:
            resources.push_async_callback(engine.dispose)
            resources.push_async_callback(bot.session.close)
            get_token_cipher()
            await check_database()
            await initialize_stt()
            await setup_bot_menu()

            worker = asyncio.create_task(run_worker(DBTokenStorage(), on_result=notify_queue_result),
                                         name="calendar worker")
            polling = asyncio.create_task(
                dp.start_polling(bot, handle_signals=False, close_bot_session=False),
                name="Telegram polling",
            )
            for task in (worker, polling):
                task.add_done_callback(background_stopped)
            try:
                yield
            finally:
                for task in (worker, polling):
                    task.remove_done_callback(background_stopped)
                worker.cancel()
                if not polling.done():
                    try:
                        await asyncio.wait_for(dp.stop_polling(), timeout=10)
                    except (RuntimeError, TimeoutError):
                        polling.cancel()
                await asyncio.gather(worker, polling, return_exceptions=True)

    frontend = Path(__file__).with_name("miniapp")
    if not frontend.is_dir():
        frontend = Path(__file__).with_name("docs")
    server = uvicorn.Server(uvicorn.Config(
        create_app(lifespan=lifespan, miniapp_api=api, frontend=frontend),
        host=os.getenv("OAUTH_HOST") or "0.0.0.0",
        port=int(os.getenv("OAUTH_PORT") or "8080"),
        workers=1,
        lifespan="on",
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=15,
    ))
    await server.serve()
    if background_failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
