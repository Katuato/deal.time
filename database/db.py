from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy import ForeignKey, String, BigInteger, Column, Integer, DateTime, Boolean, JSON

DB_URL = (
    'postgresql+asyncpg://hollywood:1234567890'
    '@localhost:5432/deal.time'
)
engine = create_async_engine(DB_URL, echo=True)
async_session = async_sessionmaker(bind=engine, expire_on_commit=False)
session = async_session()

class Base(AsyncAttrs, DeclarativeBase):
    pass
class Consent(Base):
    __tablename__ = 'consents'
    id = Column(Integer, index=True, primary_key=True)
    user_id = Column(BigInteger)
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
    google_token = Column(String)
    business_token = Column(String)
    status = Column(Boolean)
class Chat(Base):
    __tablename__ = 'chats'
    id = Column(Integer, index=True, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), unique=True)
    chat_id = Column(BigInteger)
    chat_title = Column(String)
class Event(Base):
    __tablename__ = 'events'
    id = Column(Integer, index=True, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    chat_id = Column(BigInteger, ForeignKey("chats.chat_id"))
    calendar_conn_id = '' #непонятно
    source_msg_id = '' #непонятно зачем
    title = Column(String)
    location = Column(String)
    starts_at = Column(DateTime)
    ends_at = Column(DateTime)
    duration = '' #непонятно 
    timezone = Column(String)
    repeating = Column('не знаю что тут за тип') #rrule
    participants = Column(JSON)
    status = Column(String)
    confirmed_at = Column(DateTime)
    google_event_id = Column(String)
class Operation(Base):
    __tablename__ = 'operations'
    id = Column(Integer, index=True, primary_key=True)
    event_id = Column(BigInteger) #ForeignKey("с чем связано то")
    action = Column(String)
    '''data = Column(JSON) #chto hranitsya???'''
    status = Column(String)
'''    attempts = Column(Integer) #chto etooooo
    next_attempt_at = Column(DateTime) #i etooooo'''



async def async_main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)