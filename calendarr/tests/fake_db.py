"""Подмена `database.db` на sqlite+aiosqlite (in-memory) для тестов без Postgres и Fernet."""
import sys
import types

from sqlalchemy import JSON, BigInteger, Column, DateTime, Integer, String
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool


def build_fake_db() -> types.ModuleType:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    class Base(AsyncAttrs, DeclarativeBase):
        pass

    class Event(Base):
        __tablename__ = "events"
        id = Column(Integer, primary_key=True)
        user_id = Column(BigInteger)
        title = Column(String)
        status = Column(String)
        confirmed_at = Column(DateTime)
        google_event_id = Column(String)
        google_html_link = Column(String)

    class Operation(Base):
        __tablename__ = "operations"
        id = Column(Integer, primary_key=True)
        event_id = Column(BigInteger)
        action = Column(String)
        data = Column(JSON)
        status = Column(String)
        attempts = Column(Integer)
        next_attempt_at = Column(DateTime)

    mod = types.ModuleType("database.db")
    mod.engine = engine
    mod.Base, mod.Event, mod.Operation = Base, Event, Operation
    mod.async_session = async_sessionmaker(bind=engine, expire_on_commit=False)
    return mod


def install() -> types.ModuleType:
    """Ставит фейк в sys.modules (если ещё не стоит). Вызывать до импорта calendarr.calendar_queue."""
    if "database.db" not in sys.modules or not hasattr(sys.modules["database.db"], "Operation"):
        pkg = sys.modules.setdefault("database", types.ModuleType("database"))
        pkg.__path__ = getattr(pkg, "__path__", [])
        mod = build_fake_db()
        sys.modules["database.db"] = mod
        pkg.db = mod
    return sys.modules["database.db"]
