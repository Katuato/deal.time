from sqlalchemy import update, delete, select
from typing import Optional
from datetime import datetime
from database.db import async_session, Consent, User, Connection, Chat, Event, Operation

async def get_user(user_id: int):
    '''получение пользователя'''
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        return user
#добавляем пользоователя только после выбора таймзоны, или сразу?
async def add_user(user_id: int, ):
    '''добавление пользователя'''
    async with async_session() as session:
        user = await get_user(user_id)
        if not user:
            action = session.add(User(user_id=user_id))
            return action
        return user
async def del_user(user_id: int):
    '''удаление пользователя'''
    async with async_session() as session:
        await session.execute(delete(User).where(User.user_id == user_id))
        await session.commit()

async def add_consent(user_id: int, policy_version: str, accepted_at: datetime, revoked_at: Optional[datetime] = None):
    '''добавление согласия пользователя'''
    async with async_session() as session:
        try:
            session.add(Consent(user_id=user_id, policy_version=policy_version,
                                accepted_at=accepted_at, revoked_at=revoked_at))
            await session.commit()
        except: await session.rollback()
async def get_consent(user_id: int):
    '''получение информации о согласии'''
    async with async_session() as session:
        result = (await session.execute(select(Consent).where(Consent.user_id == user_id))).scalar_one_or_none()
        return result

async def add_buss_conn(user_id: int, business_token: Optional[str] = None, google_token: Optional[str] = None, status: Optional[bool] = None):
    '''добавление/изменение информации о подключении'''
    async with async_session() as session:
        try:
            result = await get_buss_conn(user_id=user_id)
            if not result:
                if business_token is not None: session.add(Connection(user_id=user_id, business_token=business_token, status=status))
                if google_token is not None: session.add(Connection(user_id=user_id, google_token=google_token))
            else:
                if business_token is not None: await session.execute(update(Connection).where(Connection.user_id == user_id).values(business_token=business_token, status=status))
                if google_token is not None: await session.execute(update(Connection).where(Connection.user_id == user_id).values(google_token=google_token))
            await session.commit()
        except Exception as e:
            print(f"Ошибка в add_buss_conn:\n", e, "\n\n\n")
            return False
        else: return True
async def get_buss_conn(user_id: int):
    '''получение информации о бизнес подключении'''
    async with async_session() as session:
        result = (await session.execute(select(Connection).where(Connection.user_id == user_id))).scalar_one_or_none()
        return result
