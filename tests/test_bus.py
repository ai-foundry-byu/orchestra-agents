"""Tests for orchestra.bus over both MemoryStore and SqliteStore."""

import pytest

from orchestra.bus import (
    DeliveryFailed,
    MemoryStore,
    Message,
    MessageBus,
    MessageStatus,
    SqliteStore,
    UnknownRecipient,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryStore()
    return SqliteStore(tmp_path / "bus.db")


def test_post_is_durable_before_return(store):
    bus = MessageBus(store)
    mid = bus.post("worker", "do the thing", from_agent="orchestrator")
    msg = bus.get(mid)
    assert msg is not None
    assert msg.status == MessageStatus.PENDING
    assert msg.body == "do the thing"


def test_inbox_returns_pending(store):
    bus = MessageBus(store)
    bus.post("worker", "task 1", from_agent="orch")
    bus.post("worker", "task 2", from_agent="orch")
    bus.post("other", "task 3", from_agent="orch")
    inbox = bus.inbox("worker")
    assert len(inbox) == 2
    assert [m.body for m in inbox] == ["task 1", "task 2"]


def test_claim_respond_flow(store):
    bus = MessageBus(store)
    mid = bus.post("worker", "compute", from_agent="orch")
    msg = bus.claim(mid)
    assert msg.status == MessageStatus.PROCESSING
    bus.respond(mid, "42")
    done = bus.get(mid)
    assert done.status == MessageStatus.DONE
    assert done.response == "42"


def test_ask_happy_path(store):
    bus = MessageBus(store)
    answer = bus.ask(
        "worker", "2+2?", from_agent="orch", handler=lambda m: "4"
    )
    assert answer == "4"


def test_ask_empty_response_dead_letters(store):
    bus = MessageBus(store)
    with pytest.raises(DeliveryFailed):
        bus.ask("worker", "?", from_agent="orch", handler=lambda m: "")


def test_ask_handler_exception_dead_letters(store):
    bus = MessageBus(store)
    with pytest.raises(DeliveryFailed):
        bus.ask(
            "worker", "?", from_agent="orch",
            handler=lambda m: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    # the message must be recorded DEAD, not silently gone
    inbox_all = store.inbox("worker", (MessageStatus.DEAD,))
    assert len(inbox_all) == 1


def test_unknown_recipient_refused():
    bus = MessageBus(MemoryStore(), known_agents={"worker"})
    with pytest.raises(UnknownRecipient):
        bus.post("ghost", "hi", from_agent="orch")


def test_register_agent():
    bus = MessageBus(MemoryStore(), known_agents={"worker"})
    bus.register("newbie")
    mid = bus.post("newbie", "hi", from_agent="orch")
    assert bus.get(mid) is not None


def test_sqlite_survives_new_connection(tmp_path):
    path = tmp_path / "bus.db"
    bus1 = MessageBus(SqliteStore(path))
    mid = bus1.post("worker", "durable", from_agent="orch")
    # fresh store object over the same file == process restart
    bus2 = MessageBus(SqliteStore(path))
    msg = bus2.get(mid)
    assert msg is not None
    assert msg.body == "durable"
