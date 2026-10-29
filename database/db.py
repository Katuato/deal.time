from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy import String, BigInteger, Column, Integer, DateTime, Boolean

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
    user_id = Column(BigInteger, unique=True)
    google_token = Column(String)
    business_token = Column(String)
    status = Column(Boolean)

async def async_main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)