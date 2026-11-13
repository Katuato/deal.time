import logging
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Update

# Контекст текущего апдейта: пользователь и событие
ctx_user: ContextVar[str] = ContextVar("ctx_user", default="-")
ctx_event: ContextVar[str] = ContextVar("ctx_event", default="-")

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | user=%(user)s | event=%(event)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: int = logging.INFO, log_dir: str = "logs") -> None:
    # 1. Каждая запись (включая логи aiogram) получает user и event
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.user = ctx_user.get()
        record.event = ctx_event.get()
        return record

    logging.setLogRecordFactory(record_factory)

    # 2. Хендлеры
    Path(log_dir).mkdir(exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    # Все логи INFO+ -> logs/bot.log (до 10 МБ, 5 старых файлов)
    file_all = RotatingFileHandler(
        f"{log_dir}/bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_all.setFormatter(formatter)

    # Только ошибки -> logs/errors.log
    file_errors = RotatingFileHandler(
        f"{log_dir}/errors.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_errors.setLevel(logging.ERROR)
    file_errors.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=[console, file_all, file_errors], force=True)

    # 3. Приглушаем шумные библиотеки
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _describe_event(update: Update) -> str:
    if update.message:
        m = update.message
        text = m.text or m.caption or str(m.content_type)
        return f"message:{text[:50]}"
    if update.callback_query:
        return f"callback:{update.callback_query.data}"
    return str(update.event_type)  # inline_query, my_chat_member и т.д.


class LoggingContextMiddleware(BaseMiddleware):
    """Запоминает пользователя и событие для всех логов этого апдейта.

    Подключение в main.py:
        setup_logging()  # до всего остального
        dp.update.outer_middleware(LoggingContextMiddleware())
    """

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            ctx_user.set(f"{user.id}(@{user.username})" if user.username else str(user.id))
        ctx_event.set(_describe_event(event))
        return await handler(event, data)
