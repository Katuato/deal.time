import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from database.db import async_session, Consent, User, Connection, Chat, Event, Operation

logger = logging.getLogger(__name__)

# Единые правила для всех функций:
#   get_*            -> объект или None (None также при ошибке)
#   add_* / edit_* / update_* / del_* -> True/False (add_event -> id события или None)


# ---------- Пользователи ----------

async def get_user(user_id: int):
    '''получение пользователя, None если не найден'''
    async with async_session() as session:
        try:
            return (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        except Exception:
            logger.exception("Ошибка в get_user (user_id=%s)", user_id)
            return None


# пользователь добавляется сразу после согласия, часовой пояс записывается следующим шагом (edit_user)
async def add_user(user_id: int):
    '''добавление пользователя, если его ещё нет. Возвращает True/False'''
    async with async_session() as session:
        try:
            user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
            if user is None:
                session.add(User(user_id=user_id))
                await session.commit()
            return True
        except Exception:
            await session.rollback()
            logger.exception("Ошибка в add_user (user_id=%s)", user_id)
            return False


async def notifications_enabled(user_id: int) -> bool:
    user = await get_user(user_id)
    return user is None or user.notification is not False


USER_FIELDS = {"timezone", "notification"}


async def edit_user(user_id: int, **fields):
    '''изменение пользователя: передаются только те поля, которые нужно поменять. Возвращает True/False'''
    unknown = set(fields) - USER_FIELDS
    if unknown:
        raise ValueError(f"Неизвестные поля пользователя: {unknown}")
    if not fields:
        return False
    async with async_session() as session:
        try:
            result = await session.execute(update(User).where(User.user_id == user_id).values(**fields))
            await session.commit()
            return result.rowcount > 0
        except Exception:
            await session.rollback()
            logger.exception("Ошибка в edit_user (user_id=%s)", user_id)
            return False


async def del_user(user_id: int):
    '''удаление пользователя. Возвращает True, если пользователь был удалён'''
    async with async_session() as session:
        try:
            result = await session.execute(delete(User).where(User.user_id == user_id))
            await session.commit()
            return result.rowcount > 0
        except Exception:
            await session.rollback()
            logger.exception("Ошибка в del_user (user_id=%s)", user_id)
            return False


# ---------- Согласия ----------

CONSENT_FIELDS = {"policy_version", "accepted_at", "revoked_at"}


async def get_consent(user_id: int):
    '''получение информации о согласии, None если не найдено'''
    async with async_session() as session:
        try:
            return (await session.execute(select(Consent).where(Consent.user_id == user_id))).scalar_one_or_none()
        except Exception:
            logger.exception("Ошибка в get_consent (user_id=%s)", user_id)
            return None


async def has_active_consent(user_id: int) -> bool:
    consent = await get_consent(user_id)
    return consent is not None and consent.accepted_at is not None and consent.revoked_at is None


async def accept_consent(user_id: int, policy_version: str, accepted_at: datetime) -> bool | None:
    """Сохраняет согласие и создаёт профиль в одной транзакции."""
    try:
        async with async_session.begin() as session:
            await session.execute(
                insert(User).values(user_id=user_id).on_conflict_do_nothing(index_elements=[User.user_id])
            )
            statement = insert(Consent).values(user_id=user_id, policy_version=policy_version,
                                               accepted_at=accepted_at, revoked_at=None)
            result = await session.execute(statement.on_conflict_do_update(
                index_elements=[Consent.user_id],
                set_={"policy_version": policy_version, "accepted_at": accepted_at, "revoked_at": None},
                where=or_(Consent.policy_version.is_distinct_from(policy_version),
                          Consent.accepted_at.is_(None), Consent.revoked_at.is_not(None)),
            ))
            return result.rowcount > 0
    except Exception:
        logger.exception("Ошибка сохранения согласия (user_id=%s)", user_id)
        return None


async def add_consent(user_id: int, policy_version: str, accepted_at: datetime,
                      revoked_at: Optional[datetime] = None):
    '''добавление согласия пользователя. Возвращает True/False'''
    async with async_session() as session:
        try:
            session.add(Consent(user_id=user_id, policy_version=policy_version,
                                accepted_at=accepted_at, revoked_at=revoked_at))
            await session.commit()
            return True
        except Exception:
            await session.rollback()
            logger.exception("Ошибка в add_consent (user_id=%s)", user_id)
            return False


async def edit_consent(user_id: int, **fields):
    '''изменение согласия: передаются только те поля, которые нужно поменять. Возвращает True/False'''
    unknown = set(fields) - CONSENT_FIELDS
    if unknown:
        raise ValueError(f"Неизвестные поля согласия: {unknown}")
    if not fields:
        return False
    async with async_session() as session:
        try:
            result = await session.execute(update(Consent).where(Consent.user_id == user_id).values(**fields))
            await session.commit()
            return result.rowcount > 0
        except Exception:
            await session.rollback()
            logger.exception("Ошибка в edit_consent (user_id=%s)", user_id)
            return False


# ---------- Подключения ----------

async def get_buss_conn(user_id: int):
    '''получение информации о подключении, None если не найдено'''
    async with async_session() as session:
        try:
            return (await session.execute(select(Connection).where(Connection.user_id == user_id))).scalar_one_or_none()
        except Exception:
            logger.exception("Ошибка в get_buss_conn (user_id=%s)", user_id)
            return None


async def add_buss_conn(user_id: int, business_token: Optional[str] = None,
                        google_token: Optional[str] = None, status: Optional[bool] = None):
    '''добавление/изменение информации о подключении. Возвращает True/False.
    status меняется вместе с business_token'''
    async with async_session() as session:
        try:
            conn = (await session.execute(select(Connection).where(Connection.user_id == user_id))).scalar_one_or_none()
            if conn is None:
                session.add(Connection(user_id=user_id, business_token=business_token,
                                       google_token=google_token, status=status))
            else:
                values = {}
                if business_token is not None:
                    values.update(business_token=business_token, status=status)
                if google_token is not None:
                    values["google_token"] = google_token
                if values:
                    await session.execute(update(Connection).where(Connection.user_id == user_id).values(**values))
            await session.commit()
            return True
        except Exception:
            await session.rollback()
            logger.exception("Ошибка в add_buss_conn (user_id=%s)", user_id)
            return False


# ---------- Чаты Telegram Business ----------

async def record_business_chat(user_id: int, chat_id: int, chat_title: str | None) -> None:
    try:
        async with async_session.begin() as session:
            statement = insert(Chat).values(user_id=user_id, chat_id=chat_id, chat_title=chat_title)
            await session.execute(statement.on_conflict_do_update(
                index_elements=[Chat.user_id, Chat.chat_id],
                set_={"chat_title": statement.excluded.chat_title},
                where=(statement.excluded.chat_title.is_not(None)
                       & Chat.chat_title.is_distinct_from(statement.excluded.chat_title)),
            ))
    except Exception:
        logger.exception("Не удалось записать чат Telegram Business (user_id=%s)", user_id)


async def get_business_chats(user_id: int, *, offset: int, limit: int) -> list[Chat] | None:
    try:
        async with async_session() as session:
            statement = (
                select(Chat)
                .where(Chat.user_id == user_id, Chat.chat_id.is_not(None))
                .order_by(Chat.id)
                .offset(offset)
                .limit(limit)
            )
            return list((await session.scalars(statement)).all())
    except Exception:
        logger.exception("Не удалось получить чаты Telegram Business (user_id=%s)", user_id)
        return None


# ---------- События ----------

EVENT_FIELDS = {"title", "location", "starts_at", "ends_at", "duration", "timezone",
                "participants", "status", "confirmed_at", "google_event_id", "rrule"}


async def get_event(event_id: int):
    '''получение события по id, None если не найдено'''
    async with async_session() as session:
        try:
            return (await session.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
        except Exception:
            logger.exception("Ошибка в get_event (event_id=%s)", event_id)
            return None


async def add_event(user_id: int, chat_id: int, title: str, starts_at: datetime, timezone: str, status: str = "draft",
                    duration: Optional[timedelta] = None, ends_at: Optional[datetime] = None,
                    location: Optional[str] = None, participants: Optional[list[dict[str, Any]]] = None,
                    confirmed_at: Optional[datetime] = None, google_event_id: Optional[str] = None,
                    rrule: Optional[str] = None):
    '''добавление события, возвращает id события или None при ошибке'''
    async with async_session() as session:
        try:
            event = Event(user_id=user_id, chat_id=chat_id, title=title, starts_at=starts_at, timezone=timezone,
                          status=status, duration=duration, ends_at=ends_at, location=location,
                          participants=participants or [], confirmed_at=confirmed_at, google_event_id=google_event_id,
                          rrule=rrule)
            session.add(event)
            await session.flush()
            event_id = event.id
            await session.commit()
            return event_id
        except Exception:
            await session.rollback()
            logger.exception("Ошибка в add_event (user_id=%s)", user_id)
            return None


async def update_event(event_id: int, *, draft_user_id: int | None = None, **fields):
    '''изменение события: передаются только те поля, которые нужно поменять. Возвращает True/False.
    draft_user_id — менять только черновик (status=draft) этого пользователя'''
    unknown = set(fields) - EVENT_FIELDS
    if unknown:
        raise ValueError(f"Неизвестные поля события: {unknown}")
    if not fields:
        return False
    async with async_session() as session:
        try:
            if "participants" in fields and fields["participants"] is None:
                fields["participants"] = []  # None записался бы как JSON null, а участники — всегда список
            statement = update(Event).where(Event.id == event_id)
            if draft_user_id is not None:
                statement = statement.where(Event.user_id == draft_user_id, Event.status == "draft")
            result = await session.execute(statement.values(**fields))
            await session.commit()
            return result.rowcount > 0
        except Exception:
            await session.rollback()
            logger.exception("Ошибка в update_event (event_id=%s)", event_id)
            return False
