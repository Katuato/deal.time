"""
Реализация TokenStorageProtocol для таблицы `connections`.
Поле `google_token` имеет тип EncryptedString (Fernet на уровне TypeDecorator),
поэтому здесь только JSON-сериализация контракта `Credentials.to_json()`.
"""
import json
import logging
from typing import Any

from sqlalchemy import select, update

from database.db import async_session, Connection

logger = logging.getLogger(__name__)


class DBTokenStorage:
    async def get_user_token(self, user_id: int) -> dict[str, Any] | None:
        async with async_session() as session:
            row = (
                await session.execute(
                    select(Connection.google_token).where(Connection.user_id == user_id)
                )
            ).scalar_one_or_none()
        if not row:
            return None
        try:
            return json.loads(row)
        except (TypeError, ValueError) as err:
            logger.error("Битый JSON в google_token у user_id=%s: %s", user_id, err)
            return None

    async def save_user_token(self, user_id: int, token_data: dict[str, Any]) -> None:
        payload = json.dumps(token_data, ensure_ascii=False)
        async with async_session() as session:
            try:
                existing_id = (
                    await session.execute(
                        select(Connection.id).where(Connection.user_id == user_id)
                    )
                ).scalar_one_or_none()
                if existing_id is None:
                    session.add(Connection(user_id=user_id, google_token=payload))
                else:
                    await session.execute(
                        update(Connection)
                        .where(Connection.user_id == user_id)
                        .values(google_token=payload)
                    )
                await session.commit()
            except Exception as err:
                await session.rollback()
                logger.exception("Не удалось сохранить google_token user_id=%s: %s", user_id, err)
                raise

    async def revoke_user_token(self, user_id: int) -> None:
        """152-ФЗ: очистка токена без удаления строки (business_token сохраняется)."""
        async with async_session() as session:
            try:
                await session.execute(
                    update(Connection)
                    .where(Connection.user_id == user_id)
                    .values(google_token=None)
                )
                await session.commit()
            except Exception as err:
                await session.rollback()
                logger.exception("Не удалось отозвать google_token user_id=%s: %s", user_id, err)
                raise