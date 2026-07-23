"""orchestra.bus — a durable, receipted inter-agent message bus.

Why durability matters
----------------------
A naive inter-agent call is a synchronous RPC with no record: if the transport
blips it returns an empty string that is indistinguishable from "the agent said
nothing." That silent-failure class is how a message bus looks alive while
quietly dropping everything.

This bus persists every message to durable storage BEFORE delivery, so nothing
can evaporate, every exchange is inspectable, and a failure is dead-lettered
rather than silently swallowed. A message walks a strict status machine:

    pending -> processing -> done | failed | dead

Storage is pluggable. The default :class:`SqliteStore` gives real
crash-durability with zero external services (survives a process crash — not a
full-machine outage, which would need replication). :class:`MemoryStore` is for
tests. The bus logic is identical over either.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol


class MessageStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


TERMINAL = {MessageStatus.DONE, MessageStatus.FAILED, MessageStatus.DEAD}


@dataclass
class Message:
    id: str
    to_agent: str
    from_agent: str
    body: str
    kind: str = "task"
    status: MessageStatus = MessageStatus.PENDING
    response: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "to_agent": self.to_agent,
            "from_agent": self.from_agent,
            "body": self.body,
            "kind": self.kind,
            "status": self.status.value,
            "response": self.response,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Message":
        return cls(
            id=row["id"],
            to_agent=row["to_agent"],
            from_agent=row["from_agent"],
            body=row["body"],
            kind=row.get("kind", "task"),
            status=MessageStatus(row["status"]),
            response=row.get("response"),
            error=row.get("error"),
            created_at=row.get("created_at", time.time()),
            updated_at=row.get("updated_at", time.time()),
        )


# ── storage protocol ──────────────────────────────────────────────────────
class Store(Protocol):
    def insert(self, msg: Message) -> None: ...
    def update(self, msg: Message) -> None: ...
    def get(self, msg_id: str) -> Message | None: ...
    def inbox(self, agent: str, statuses: tuple[MessageStatus, ...]) -> list[Message]: ...


class MemoryStore:
    """In-memory store for tests. Not durable."""

    def __init__(self) -> None:
        self._rows: dict[str, Message] = {}
        self._lock = threading.Lock()

    def insert(self, msg: Message) -> None:
        with self._lock:
            self._rows[msg.id] = msg

    def update(self, msg: Message) -> None:
        with self._lock:
            msg.updated_at = time.time()
            self._rows[msg.id] = msg

    def get(self, msg_id: str) -> Message | None:
        with self._lock:
            return self._rows.get(msg_id)

    def inbox(self, agent: str, statuses: tuple[MessageStatus, ...]) -> list[Message]:
        with self._lock:
            out = [
                m
                for m in self._rows.values()
                if m.to_agent == agent and m.status in statuses
            ]
        return sorted(out, key=lambda m: m.created_at)


class SqliteStore:
    """Durable SQLite store. Survives process crashes."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS agent_messages (
        id TEXT PRIMARY KEY,
        to_agent TEXT NOT NULL,
        from_agent TEXT NOT NULL,
        body TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        response TEXT,
        error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_inbox ON agent_messages(to_agent, status);
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path).expanduser())
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(self._DDL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def insert(self, msg: Message) -> None:
        r = msg.to_row()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages "
                "(id,to_agent,from_agent,body,kind,status,response,error,created_at,updated_at) "
                "VALUES (:id,:to_agent,:from_agent,:body,:kind,:status,:response,:error,"
                ":created_at,:updated_at)",
                r,
            )

    def update(self, msg: Message) -> None:
        msg.updated_at = time.time()
        r = msg.to_row()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE agent_messages SET status=:status, response=:response, "
                "error=:error, updated_at=:updated_at WHERE id=:id",
                r,
            )

    def get(self, msg_id: str) -> Message | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_messages WHERE id=?", (msg_id,)
            ).fetchone()
        return Message.from_row(dict(row)) if row else None

    def inbox(self, agent: str, statuses: tuple[MessageStatus, ...]) -> list[Message]:
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_messages WHERE to_agent=? AND status IN ({placeholders}) "
                "ORDER BY created_at ASC",
                (agent, *[s.value for s in statuses]),
            ).fetchall()
        return [Message.from_row(dict(r)) for r in rows]


class UnknownRecipient(Exception):
    """Raised when a message targets an unregistered agent (never fabricated)."""


class DeliveryFailed(Exception):
    """Raised by ask() when a message dead-letters instead of completing."""


class MessageBus:
    """Durable message bus.

    * :meth:`post` — fire-and-forget durable send; row lands *pending* before return.
    * :meth:`ask` — durable RPC: post, deliver, record the response, or dead-letter
      and raise (never silently returns empty).
    * Target side: :meth:`inbox`, :meth:`claim`, :meth:`respond`, :meth:`fail`.

    A registry of known agents is enforced: an unknown recipient dead-letters
    loudly rather than being invented.
    """

    def __init__(
        self,
        store: Store | None = None,
        *,
        known_agents: set[str] | None = None,
    ) -> None:
        self.store = store or MemoryStore()
        self.known_agents = known_agents

    def register(self, agent: str) -> None:
        if self.known_agents is None:
            self.known_agents = set()
        self.known_agents.add(agent)

    def _check_recipient(self, to_agent: str) -> None:
        if self.known_agents is not None and to_agent not in self.known_agents:
            raise UnknownRecipient(
                f"unknown recipient '{to_agent}' — refusing to fabricate a target"
            )

    # ── producer side ────────────────────────────────────────────────────
    def post(self, to_agent: str, body: str, *, from_agent: str, kind: str = "task") -> str:
        self._check_recipient(to_agent)
        msg = Message(
            id=uuid.uuid4().hex, to_agent=to_agent, from_agent=from_agent, body=body, kind=kind
        )
        self.store.insert(msg)  # durable BEFORE anything else
        return msg.id

    def ask(
        self,
        to_agent: str,
        body: str,
        *,
        from_agent: str,
        handler: Callable[[Message], str],
        kind: str = "task",
    ) -> str:
        """Durable RPC. ``handler`` is the target's work function.

        On any handler exception the message is dead-lettered and DeliveryFailed
        is raised — the caller can never mistake a failure for an empty answer.
        """
        msg_id = self.post(to_agent, body, from_agent=from_agent, kind=kind)
        msg = self.claim(msg_id)
        try:
            answer = handler(msg)
            if not answer:
                raise DeliveryFailed("handler returned empty response")
            self.respond(msg_id, answer)
            return answer
        except Exception as exc:  # noqa: BLE001
            self.fail(msg_id, str(exc), dead=True)
            raise DeliveryFailed(
                f"message {msg_id} to '{to_agent}' dead-lettered: {exc}"
            ) from exc

    # ── consumer side ────────────────────────────────────────────────────
    def inbox(self, agent: str, *, pending_only: bool = True) -> list[Message]:
        statuses = (
            (MessageStatus.PENDING,)
            if pending_only
            else (MessageStatus.PENDING, MessageStatus.PROCESSING)
        )
        return self.store.inbox(agent, statuses)

    def claim(self, msg_id: str) -> Message:
        msg = self.store.get(msg_id)
        if msg is None:
            raise KeyError(f"no such message: {msg_id}")
        msg.status = MessageStatus.PROCESSING
        self.store.update(msg)
        return msg

    def respond(self, msg_id: str, response: str) -> None:
        msg = self.store.get(msg_id)
        if msg is None:
            raise KeyError(msg_id)
        msg.status = MessageStatus.DONE
        msg.response = response
        self.store.update(msg)

    def fail(self, msg_id: str, error: str, *, dead: bool = False) -> None:
        msg = self.store.get(msg_id)
        if msg is None:
            raise KeyError(msg_id)
        msg.status = MessageStatus.DEAD if dead else MessageStatus.FAILED
        msg.error = error
        self.store.update(msg)

    def get(self, msg_id: str) -> Message | None:
        return self.store.get(msg_id)
