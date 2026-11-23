import os
from functools import lru_cache

import sqlalchemy as sa
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy import ForeignKey, String, BigInteger, Column, Integer, DateTime, Boolean, JSON, Index

load_dotenv()


def get_database_url() -> URL:
    value = os.getenv("DATABASE_URL")
    if not value:
        credentials = os.getenv("db_logpass")
        host = os.getenv("db_ipport")
        if not credentials or not host:
            raise RuntimeError("Задайте DATABASE_URL или db_logpass и db_ipport")
        value = f"postgresql+asyncpg://{credentials}{host}/deal.time"

    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise RuntimeError("Некорректный адрес PostgreSQL") from None
    if url.drivername in {"postgres", "postgresql"}:
        return url.set(drivername="postgresql+asyncpg")
    if url.drivername != "postgresql+asyncpg":
        raise RuntimeError("Для DATABASE_URL требуется PostgreSQL с драйвером asyncpg")
    return url


engine = create_async_engine(get_database_url(), pool_pre_ping=True, hide_parameters=True)
async_session = async_sessionmaker(bind=engine, expire_on_commit=False)


@lru_cache(maxsize=1)
def get_token_cipher() -> Fernet:
    key = os.getenv("secret_key")
    if not key:
        raise RuntimeError("Задайте secret_key для шифрования токенов")
    try:
        return Fernet(key)
    except ValueError:
        raise RuntimeError("Некорректный ключ шифрования токенов") from None


class EncryptedString(sa.TypeDecorator):
    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return get_token_cipher().encrypt(value.encode('utf-8')).decode('utf-8')
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return get_token_cipher().decrypt(value.encode('utf-8')).decode('utf-8')
        return value
    
class Base(AsyncAttrs, DeclarativeBase):
    pass
class Consent(Base):
    __tablename__ = 'consents'
    id = Column(Integer, index=True, primary_key=True)
    user_id = Column(BigInteger, unique=True)
    policy_version = Column(String)
    accepted_at = Column(DateTime)
    revoked_at = Column(DateTime, default=None)
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, index=True, primary_key=True)
    user_id = Column(BigInteger, unique=True)
    timezone = Column(String)
    notification = Column(Boolean)
class Connection(Base):
    __tablename__ = 'connections'
    id = Column(Integer, index=True, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), unique=True)
    google_token = Column(EncryptedString, default=None)
    business_token = Column(EncryptedString, default=None)
    status = Column(Boolean, default=None)
class Chat(Base):
    __tablename__ = 'chats'
    id = Column(Integer, index=True, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), unique=True)
    chat_id = Column(BigInteger)
    chat_title = Column(String, default=None)
class Event(Base):
    __tablename__ = 'events'
    id = Column(Integer, index=True, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    chat_id = Column(BigInteger)
    title = Column(String)
    location = Column(String)
    starts_at = Column(DateTime)
    ends_at = Column(DateTime)
    duration = Column(String)
    timezone = Column(String)
    repeating = Column(String) #rrule
    participants = Column(JSON)
    status = Column(String)
    confirmed_at = Column(DateTime)
    google_event_id = Column(String)
class Operation(Base):
    __tablename__ = 'operations'
    __table_args__ = (
        Index(
            "ix_operations_due", "next_attempt_at", "id",
            postgresql_where=sa.text("status IN ('pending', 'processing')"),
        ),
        Index("ix_operations_event_action_status", "event_id", "action", "status"),
    )
    id = Column(Integer, index=True, primary_key=True)
    event_id = Column(BigInteger)
    action = Column(String)
    data = Column(JSON)
    status = Column(String)
    attempts = Column(Integer, default=0)
    next_attempt_at = Column(DateTime)
    next_attempt_step = Column(JSON, default=lambda: [10, 20, 60])


async def check_database() -> None:
    async with engine.connect() as connection:
        revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version LIMIT 1"))
        if revision is None:
            raise RuntimeError("Выполните миграции базы данных")
        await connection.execute(sa.select(Operation.id, Event.id).select_from(
            Operation.__table__.join(Event, Operation.event_id == Event.id)
        ).limit(0))
