"""
OAuth 2.0 flow для Google Calendar.
Получение первичного refresh_token для пользователя.
"""
import asyncio
import json
import logging
import secrets
import time
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
from calendarr.calendar_connector import SCOPES

logger = logging.getLogger(__name__)


_CLIENT_CONFIG = {
    "web": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [GOOGLE_REDIRECT_URI],
    }
}


# Реестр state -> (user_id, code_verifier, created_monotonic).
# Хранится в памяти процесса (бот работает одним процессом); при рестарте
# незавершённые авторизации теряются — пользователю нужно нажать кнопку заново.
STATE_TTL_SECONDS = 600  # 10 минут
_pending_states: dict[str, tuple[int, str | None, float]] = {}


def _purge_expired_states() -> None:
    now = time.monotonic()
    for key in [k for k, (_, _, ts) in _pending_states.items() if now - ts > STATE_TTL_SECONDS]:
        _pending_states.pop(key, None)


def _pop_state(state: str) -> tuple[int, str | None]:
    """Одноразовое получение state: удаляет запись. ValueError, если неизвестен/просрочен."""
    _purge_expired_states()
    entry = _pending_states.pop(state, None) if state else None
    if entry is None:
        raise ValueError("Неизвестный или просроченный state. Запросите ссылку заново.")
    user_id, code_verifier, _ = entry
    return user_id, code_verifier


def cancel_auth(user_id: int) -> None:
    for key in [key for key, (owner, _, _) in _pending_states.items() if owner == user_id]:
        _pending_states.pop(key, None)


def _check_config() -> None:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise RuntimeError(
            "Google OAuth не сконфигурирован: задайте GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI в .env"
        )


def build_auth_url(user_id: int) -> str:
    """Возвращает URL для авторизации пользователя в Google Calendar."""
    _check_config()
    _purge_expired_states()

    # Случайный state (защита от CSRF): предсказуемый user_id использовать нельзя.
    state = secrets.token_urlsafe(32)
    flow = Flow.from_client_config(_CLIENT_CONFIG, scopes=SCOPES, state=state)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    # PKCE: verifier, сгенерированный Flow, нужен при обмене кода
    _pending_states[state] = (user_id, flow.code_verifier, time.monotonic())
    return url


async def exchange_code(code: str, state: str) -> tuple[int, dict[str, Any]]:
    """Обменивает authorization code на токены. Возвращает (user_id, token_dict)."""
    _check_config()
    user_id, code_verifier = _pop_state(state)

    flow = Flow.from_client_config(
        _CLIENT_CONFIG, scopes=SCOPES, state=state, code_verifier=code_verifier
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    # fetch_token — блокирующий HTTP-вызов
    await asyncio.to_thread(flow.fetch_token, code=code)
    creds: Credentials = flow.credentials

    if not creds.refresh_token:
        logger.error(
            "Google не вернул refresh_token для user_id=%s — "
            "пользователь уже авторизовался ранее без prompt=consent?",
            user_id,
        )
        raise RuntimeError(
            "Google не выдал refresh_token. Удалите доступ бота в "
            "https://myaccount.google.com/permissions и попробуйте снова."
        )

    return user_id, json.loads(creds.to_json())
