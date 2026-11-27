"""HTTP-маршруты Google OAuth и проверки готовности приложения."""
import html
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import config
from calendarr import oauth
from calendarr.storage import DBTokenStorage
from database.db import check_database
from database.models import has_active_consent

logger = logging.getLogger(__name__)

DEFAULT_CALLBACK_PATH = "/oauthcallback"


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title></head>"
        f"<body style=\"font-family:sans-serif;text-align:center;margin-top:15vh\">"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(text)}</p></body></html>"
    )
    return HTMLResponse(
        body, status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def oauth_callback(request: Request) -> HTMLResponse:
    if request.query_params.get("error"):
        logger.warning("Google OAuth вернул ошибку")
        return _page("Авторизация отменена",
                     "Google вернул ошибку или доступ не был предоставлен. "
                     "Вернитесь в бота и попробуйте снова.", 400)

    code = request.query_params.get("code")
    state = request.query_params.get("state")
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
        # то же сообщение, что на /start при всех подключениях (или следующий шаг настройки)
        from tg_bot.user_end import continue_setup  # внутри: tg_bot тянет бота и роутеры
        await continue_setup(user_id, google_just_connected=True)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить уведомление user_id=%s", user_id)

    logger.info("Google Календарь подключён для user_id=%s", user_id)
    return _page("Google Календарь подключён ✅", "Можно закрыть вкладку и вернуться в Telegram.")


def _callback_path() -> str:
    if not config.GOOGLE_REDIRECT_URI:
        return DEFAULT_CALLBACK_PATH
    callback = urlparse(config.GOOGLE_REDIRECT_URI)
    if callback.scheme not in {"http", "https"} or not callback.hostname:
        raise ValueError("Задайте полный HTTP(S)-адрес в GOOGLE_REDIRECT_URI")
    return callback.path or "/"


async def health() -> PlainTextResponse:
    try:
        await check_database()
    except Exception:
        return PlainTextResponse("unavailable", status_code=503)
    return PlainTextResponse("ok")


def create_app(
    *, lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    miniapp_api: FastAPI | None = None, frontend: Path | None = None,
) -> FastAPI:
    app = FastAPI(lifespan=lifespan, openapi_url=None, redirect_slashes=False)
    callback_path = _callback_path()
    if callback_path in {"/healthz", "/api/v1"} or callback_path.startswith("/api/v1/"):
        raise ValueError("Путь Google OAuth совпадает со служебным маршрутом")
    app.add_api_route("/healthz", health, methods=["GET", "HEAD"], include_in_schema=False)
    if miniapp_api is not None:
        app.mount("/api/v1", miniapp_api)
    if callback_path == "/" and frontend is not None:
        @app.get("/", include_in_schema=False)
        async def legacy_oauth_or_frontend(request: Request):
            if {"code", "state", "error"}.intersection(request.query_params):
                return await oauth_callback(request)
            return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        app.add_api_route(callback_path, oauth_callback, methods=["GET"], include_in_schema=False)
    if frontend is not None:
        app.mount("/", StaticFiles(directory=frontend, html=True))
    return app
