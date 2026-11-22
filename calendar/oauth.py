"""
OAuth 2.0 flow для Google Calendar.
Получение первичного refresh_token для пользователя.
"""
import json
import logging
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
from calendar_connector import SCOPES

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


def build_auth_url(user_id: int) -> str:
    """Возвращает URL для авторизации пользователя в Google Calendar."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise RuntimeError(
            "Google OAuth не сконфигурирован: задайте GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI в .env"
        )

    flow = Flow.from_client_config(_CLIENT_CONFIG, scopes=SCOPES, state=str(user_id))
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return url


def exchange_code(code: str, state: str) -> tuple[int, dict[str, Any]]:
    """Обменивает authorization code на токены. Возвращает (user_id, token_dict)."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise RuntimeError(
            "Google OAuth не сконфигурирован: задайте GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI в .env"
        )

    flow = Flow.from_client_config(_CLIENT_CONFIG, scopes=SCOPES, state=state)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    flow.fetch_token(code=code)
    creds: Credentials = flow.credentials

    try:
        user_id = int(state)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректный state: {state!r}")

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