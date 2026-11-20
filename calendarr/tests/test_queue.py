from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from calendarr.tests import fake_db

db = fake_db.install()

from calendarr import calendar_queue as q  # noqa: E402
from calendarr import calendar_connector  # noqa: E402

OK = {"status": "success", "event_id": "gid", "html_link": "x", "duplicate": False}
NET = {"status": "error", "error_type": "network", "message": "x", "need_queue": True}
BAD = {"status": "error", "error_type": "invalid", "message": "x", "need_queue": False}


@pytest.fixture(autouse=True)
async def tables():
    async with db.engine.begin() as c:
        await c.run_sync(db.Base.metadata.create_all)
    yield
    async with db.engine.begin() as c:
        await c.run_sync(db.Base.metadata.drop_all)


@pytest.fixture
def conn(monkeypatch):
    m = {n: AsyncMock(return_value=OK) for n in ("create_event", "update_event_time", "delete_event")}
    for n, f in m.items():
        monkeypatch.setattr(calendar_connector, n, f, raising=False)
    return m


async def get_op(op_id):
    async with db.async_session() as s:
        return await s.get(db.Operation, op_id)


async def enqueue_create(**kw):
    return await q.enqueue_operation(1, "create", {"event_data": {"summary": "s"}}, **kw)


async def test_claim_sets_processing_and_lease():
    op_id = await enqueue_create()
    ops = await q.claim_due_operations()
    assert [o.id for o in ops] == [op_id]
    op = await get_op(op_id)
    assert op.status == "processing"
    assert op.next_attempt_at > q._utcnow() + timedelta(minutes=4)
    assert await q.claim_due_operations() == []  # lease держит


async def test_stale_processing_reclaimed():
    op_id = await enqueue_create()
    await q.claim_due_operations()
    async with db.async_session() as s:
        op = await s.get(db.Operation, op_id)
        op.next_attempt_at = q._utcnow() - timedelta(seconds=1)
        await s.commit()
    assert len(await q.claim_due_operations()) == 1


async def test_success_done(conn):
    op_id = await enqueue_create()
    stats = await q.process_pending_calendar_operations("st")
    assert stats["success"] == 1
    assert (await get_op(op_id)).status == "done"
    conn["create_event"].assert_awaited_once_with(1, "st", {"summary": "s"})


async def test_need_queue_backoff(conn):
    conn["create_event"].return_value = NET
    op_id = await enqueue_create()
    await q.process_pending_calendar_operations("st")
    op = await get_op(op_id)
    assert op.status == "pending" and op.attempts == 1
    delta = (op.next_attempt_at - q._utcnow()).total_seconds()
    assert 25 < delta <= 30


async def test_max_attempts_failed(conn):
    conn["create_event"].return_value = NET
    op_id = await enqueue_create()
    async with db.async_session() as s:
        op = await s.get(db.Operation, op_id)
        op.attempts = q.MAX_ATTEMPTS - 1
        await s.commit()
    stats = await q.process_pending_calendar_operations("st")
    assert stats["failed"] == 1
    assert (await get_op(op_id)).status == "failed"


async def test_permanent_error_failed(conn):
    conn["create_event"].return_value = BAD
    op_id = await enqueue_create()
    await q.process_pending_calendar_operations("st")
    assert (await get_op(op_id)).status == "failed"


async def test_unexpected_exception_retries(conn):
    conn["create_event"].side_effect = RuntimeError("boom")
    op_id = await enqueue_create()
    await q.process_pending_calendar_operations("st")
    op = await get_op(op_id)
    assert op.status == "pending" and op.attempts == 1


async def test_bad_payload_failed_immediately(conn):
    async with db.async_session() as s:
        s.add(db.Operation(action="create", data={"user_id": 1}, status="pending",
                           attempts=0, next_attempt_at=q._utcnow()))
        await s.commit()
    stats = await q.process_pending_calendar_operations("st")
    assert stats["failed"] == 1
    conn["create_event"].assert_not_called()


async def test_delete_postponed_while_create_pending(conn):
    await enqueue_create(event_id=10, delay_seconds=3600)  # create ещё не готов
    del_id = await q.enqueue_operation(1, "delete", {}, event_id=10)
    stats = await q.process_pending_calendar_operations("st")
    assert stats["postponed"] == 1
    op = await get_op(del_id)
    assert op.status == "pending" and op.attempts == 0
    conn["delete_event"].assert_not_called()


async def test_google_id_saved_after_create(conn):
    async with db.async_session() as s:
        s.add(db.Event(id=10, user_id=1, title="t"))
        await s.commit()
    await enqueue_create(event_id=10)
    await q.process_pending_calendar_operations("st")
    async with db.async_session() as s:
        assert (await s.get(db.Event, 10)).google_event_id == "gid"


async def test_delete_uses_event_google_id(conn):
    async with db.async_session() as s:
        s.add(db.Event(id=11, user_id=1, google_event_id="abc"))
        await s.commit()
    await q.enqueue_operation(1, "delete", {}, event_id=11)
    await q.process_pending_calendar_operations("st")
    conn["delete_event"].assert_awaited_once_with(1, "st", "abc")


async def test_delete_without_google_id_failed(conn):
    del_id = await q.enqueue_operation(1, "delete", {}, event_id=12)
    await q.process_pending_calendar_operations("st")
    assert (await get_op(del_id)).status == "failed"


async def test_create_or_enqueue(conn):
    conn["create_event"].return_value = NET
    res = await q.create_event_or_enqueue(1, "st", {"summary": "s"})
    assert res["queued"] is True and res["need_queue"] is True
    assert (await get_op(res["operation_id"])).status == "pending"
    conn["create_event"].return_value = OK
    res = await q.create_event_or_enqueue(1, "st", {"summary": "s"})
    assert "queued" not in res
    async with db.async_session() as s:
        n = len((await s.execute(select(db.Operation))).scalars().all())
    assert n == 1
