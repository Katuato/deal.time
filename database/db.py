import base64
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy import ForeignKey, String, BigInteger, Column, Integer, DateTime, Boolean, JSON
from config import db_logpass, db_ipport, secret_key

DB_URL = (
    f'postgresql+asyncpg://{db_logpass}'
    f'{db_ipport}/deal.time'
)
engine = create_async_engine(DB_URL, echo=True)
async_session = async_sessionmaker(bind=engine, expire_on_commit=False)
SECRET_KEY = f"{secret_key}"
fernet = Fernet(SECRET_KEY)
session = async_session()

class EncryptedString(sa.TypeDecorator):
    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return fernet.encrypt(value.encode('utf-8')).decode('utf-8')
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return fernet.decrypt(value.encode('utf-8')).decode('utf-8')
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
    calendar_conn_id = '' #непонятно
    title = Column(String)
    location = Column(String)
    starts_at = Column(DateTime)
    ends_at = Column(DateTime)
    duration = '' #непонятно 
    timezone = Column(String)
    '''repeating = Column('не знаю что тут за тип') #rrule'''
    participants = Column(JSON)
    status = Column(String)
    confirmed_at = Column(DateTime)
    google_event_id = Column(String)
class Operation(Base):
    __tablename__ = 'operations'
    id = Column(Integer, index=True, primary_key=True)
    event_id = Column(BigInteger) #ForeignKey("с чем связано то")
    action = Column(String)
    data = Column(JSON)
    status = Column(String)
    attempts = Column(Integer)
    next_attempt_at = Column(DateTime)



async def async_main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)