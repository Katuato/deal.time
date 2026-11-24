import logging
import traceback
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Update

ctx_user: ContextVar[str] = ContextVar("ctx_user", default="-")
ctx_event: ContextVar[str] = ContextVar("ctx_event", default="-")

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | user=%(user)s | event=%(event)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class SafeExceptionFormatter(logging.Formatter):
    def formatException(self, exc_info) -> str:
        kind, _, frames = exc_info
        return "".join(traceback.format_tb(frames)) + (kind.__name__ if kind else "")


def _hide_exception_messages(record: logging.LogRecord) -> bool:
    # SDKs may log an exception both as a format argument and as a traceback.
    if isinstance(record.args, tuple):
        record.args = tuple(
            type(arg).__name__ if isinstance(arg, BaseException) else arg
            for arg in record.args
        )
    return True


def setup_logging(level: int = logging.INFO, log_dir: str | None = None) -> None:
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.user = ctx_user.get()
        record.event = ctx_event.get()
        return record

    logging.setLogRecordFactory(record_factory)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        path = Path(log_dir)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            path / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        ))
        errors = RotatingFileHandler(
            path / "errors.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        errors.setLevel(logging.ERROR)
        handlers.append(errors)

    formatter = SafeExceptionFormatter(LOG_FORMAT, DATE_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_hide_exception_messages)
    logging.basicConfig(level=level, handlers=handlers, force=True)

    logging.getLogger("asyncio").setLevel(logging.WARNING)


class LoggingContextMiddleware(BaseMiddleware):
    """Добавляет тип обновления и ID пользователя в журналы обработки."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        event_context = ctx_event.set(str(event.event_type))
        user_context = ctx_user.set(str(user.id) if user else "-")
        try:
            return await handler(event, data)
        finally:
            ctx_user.reset(user_context)
            ctx_event.reset(event_context)
