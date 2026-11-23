from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError

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


async def get_event(event_id):
    async with db.async_session() as s:
        return await s.get(db.Event, event_id)


async def add_waiting_event(event_id, user_id=1):
    async with db.async_session.begin() as s:
        s.add(db.Event(id=event_id, user_id=user_id, title="stored title", status="waiting"))


def event_payload(event):
    return {"summary": event.title}


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


async def test_confirmation_only_queues_owned_waiting_event_once(conn):
    await add_waiting_event(10)
    assert await q.confirm_event_and_enqueue(2, 10, event_payload) is None
    op_id = await q.confirm_event_and_enqueue(1, 10, event_payload)
    assert await q.confirm_event_and_enqueue(1, 10, event_payload) is None

    operation = await get_op(op_id)
    event = await get_event(10)
    assert event.status == "queued" and event.confirmed_at is not None
    assert operation.status == "pending" and operation.event_id == event.id
    assert operation.data["event_data"] == {"summary": "stored title"}
    assert operation.data["google_event_id"]
    conn["create_event"].assert_not_awaited()


async def test_failed_operation_insert_rolls_back_confirmation(conn):
    await add_waiting_event(10)
    with pytest.raises(StatementError):
        await q.confirm_event_and_enqueue(1, 10, lambda event: {"invalid": object()})

    event = await get_event(10)
    assert event.status == "waiting" and event.confirmed_at is None
    assert await q.claim_due_operations() == []
    conn["create_event"].assert_not_awaited()


async def test_create_reuses_saved_id_after_failure_finalizing(conn, monkeypatch):
    await add_waiting_event(10)
    op_id = await q.confirm_event_and_enqueue(1, 10, event_payload)
    saved_id = (await get_op(op_id)).data["google_event_id"]
    conn["create_event"].return_value = {**OK, "event_id": saved_id}
    with monkeypatch.context() as context:
        context.setattr(q, "_finish", AsyncMock(side_effect=RuntimeError("db disconnected")))
        with pytest.raises(RuntimeError, match="db disconnected"):
            await q.process_pending_calendar_operations("st")

    old_lease = (await get_op(op_id)).next_attempt_at
    assert (await get_event(10)).status == "queued"
    monkeypatch.setattr(q, "_utcnow", lambda: old_lease + timedelta(seconds=1))
    stats = await q.process_pending_calendar_operations("st")

    assert stats["success"] == 1
    assert (await get_op(op_id)).status == "done"
    event = await get_event(10)
    assert event.status == "confirmed" and event.google_event_id == saved_id
    calls = conn["create_event"].await_args_list
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0].args[2]["google_event_id"] == saved_id


async def test_expired_worker_cannot_finalize_reclaimed_operation(monkeypatch):
    await add_waiting_event(10)
    op_id = await q.confirm_event_and_enqueue(1, 10, event_payload)
    first, = await q.claim_due_operations()
    monkeypatch.setattr(q, "_utcnow", lambda: first.next_attempt_at + timedelta(seconds=1))
    second, = await q.claim_due_operations()

    assert not await q._finish(op_id, "done", lease_until=first.next_attempt_at, google_id="old")
    assert (await get_event(10)).google_event_id is None
    assert (await get_op(op_id)).next_attempt_at == second.next_attempt_at


async def test_permanent_create_error_marks_confirmed_request_failed(conn):
    await add_waiting_event(10)
    op_id = await q.confirm_event_and_enqueue(1, 10, event_payload)
    conn["create_event"].return_value = BAD
    stats = await q.process_pending_calendar_operations("st")
    assert stats["failed"] == 1
    assert (await get_op(op_id)).status == "failed"
    assert (await get_event(10)).status == "failed"
