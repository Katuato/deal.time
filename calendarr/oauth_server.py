"""
HTTP-сервер для redirect URI Google OAuth (aiohttp).
Принимает ?code=...&state=..., обменивает код на токен, сохраняет его и уведомляет пользователя.
"""
import html
import logging
import os
from urllib.parse import urlparse

from aiohttp import web

import config
from calendarr import oauth
from calendarr.storage import DBTokenStorage
from database.db import check_database
from database.models import has_active_consent

logger = logging.getLogger(__name__)

DEFAULT_CALLBACK_PATH = "/oauth2callback"


def _page(title: str, text: str, status: int = 200) -> web.Response:
    body = (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title></head>"
        f"<body style=\"font-family:sans-serif;text-align:center;margin-top:15vh\">"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(text)}</p></body></html>"
    )
    return web.Response(text=body, status=status, content_type="text/html",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


async def oauth_callback(request: web.Request) -> web.Response:
    if request.query.get("error"):
        logger.warning("Google OAuth вернул ошибку")
        return _page("Авторизация отменена",
                     "Google вернул ошибку или доступ не был предоставлен. "
                     "Вернитесь в бота и попробуйте снова.", 400)

    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        logger.warning("OAuth callback без code/state")
        return _page("Некорректный запрос", "Отсутствуют параметры code или state.", 400)

    try:
        user_id, token = await oauth.exchange_code(code, state)
    except ValueError:
        logger.warning("OAuth callback: некорректная ссылка")
        return _page("Ссылка недействительна", "Запросите новую ссылку в боте.", 400)
    except Exception as error:
        logger.warning("Ошибка обмена OAuth-кода (%s)", type(error).__name__)
        return _page("Не удалось подключить календарь",
                     "Произошла ошибка при обмене кода. Вернитесь в бота и попробуйте снова.", 400)

    if not await has_active_consent(user_id):
        return _page("Требуется согласие", "Подтвердите согласие через /start в боте.", 403)

    try:
        await DBTokenStorage().save_user_token(user_id, token)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось сохранить токен user_id=%s", user_id)
        return _page("Ошибка сохранения",
                     "Не удалось сохранить токен. Попробуйте позже.", 400)

    try:
        await config.bot.send_message(
            chat_id=user_id, text="Google Календарь успешно подключён ✅")
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить уведомление user_id=%s", user_id)

    logger.info("Google Календарь подключён для user_id=%s", user_id)
    return _page("Google Календарь подключён ✅", "Можно закрыть вкладку и вернуться в Telegram.")


def _callback_path() -> str:
    return urlparse(config.GOOGLE_REDIRECT_URI or "").path or DEFAULT_CALLBACK_PATH


async def health(request: web.Request) -> web.Response:
    try:
        await check_database()
    except Exception:
        return web.Response(text="unavailable", status=503)
    return web.Response(text="ok")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", health)
    app.router.add_get(_callback_path(), oauth_callback)
    return app


async def start_oauth_server(host: str | None = None, port: int | None = None) -> web.AppRunner:
    host = host or os.getenv("OAUTH_HOST", "0.0.0.0")
    port = port or int(os.getenv("OAUTH_PORT", "8080"))
    runner = web.AppRunner(create_app(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    logger.info("OAuth-сервер запущен на %s:%s (путь %s)", host, port, _callback_path())
    return runner
