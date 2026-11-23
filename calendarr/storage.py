"""
Реализация TokenStorageProtocol для таблицы `connections`.
Поле `google_token` имеет тип EncryptedString (Fernet на уровне TypeDecorator),
поэтому здесь только JSON-сериализация контракта `Credentials.to_json()`.
"""
import json
import logging
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from database.db import async_session, Connection, User

logger = logging.getLogger(__name__)


class DBTokenStorage:
    async def get_user_token(self, user_id: int) -> dict[str, Any] | None:
        try:
            async with async_session() as session:
                row = (
                    await session.execute(
                        select(Connection.google_token).where(Connection.user_id == user_id)
                    )
                ).scalar_one_or_none()
        except InvalidToken:
            # расшифровка идёт при чтении строки; ключ сменился/данные испорчены -> повторная авторизация
            logger.error("Не удалось расшифровать google_token у user_id=%s — нужна повторная авторизация", user_id)
            return None
        if not row:
            return None
        try:
            return json.loads(row)
        except (TypeError, ValueError):
            logger.error("Некорректный формат сохранённого Google-токена user_id=%s", user_id)
            return None

    async def save_user_token(self, user_id: int, token_data: dict[str, Any]) -> None:
        payload = json.dumps(token_data, ensure_ascii=False)
        async with async_session() as session:
            try:
                try:
                    await self._upsert(session, user_id, payload)
                    await session.commit()
                except IntegrityError:
                    # гонка select -> insert по unique user_id: повторяем как update
                    await session.rollback()
                    await self._upsert(session, user_id, payload)
                    await session.commit()
            except Exception as err:
                await session.rollback()
                logger.error("Не удалось сохранить Google-токен user_id=%s (%s)", user_id, type(err).__name__)
                raise

    @staticmethod
    async def _upsert(session, user_id: int, payload: str) -> None:
        existing_id = (
            await session.execute(
                select(Connection.id).where(Connection.user_id == user_id)
            )
        ).scalar_one_or_none()
        if existing_id is not None:
            await session.execute(
                update(Connection)
                .where(Connection.user_id == user_id)
                .values(google_token=payload)
            )
            return
        # Connection.user_id — FK на users.user_id: гарантируем наличие пользователя
        user_exists = (
            await session.execute(select(User.id).where(User.user_id == user_id))
        ).scalar_one_or_none()
        if user_exists is None:
            session.add(User(user_id=user_id))
        session.add(Connection(user_id=user_id, google_token=payload))
        await session.flush()

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
                logger.error("Не удалось отозвать Google-токен user_id=%s (%s)", user_id, type(err).__name__)
                raise
