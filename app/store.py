"""Storage behind one small interface. MemoryStore for local/tests, FirestoreStore for Cloud Run."""
from __future__ import annotations

import threading
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.config import get_settings
from app.models import Agent, Employee, Message, Org, Playbook, Session

T = TypeVar("T", bound=BaseModel)

COLLECTIONS: dict[type[BaseModel], str] = {
    Org: "orgs",
    Employee: "employees",
    Agent: "agents",
    Session: "sessions",
    Playbook: "playbooks",
}


class Store(Protocol):
    def put(self, obj: BaseModel) -> None: ...
    def get(self, cls: type[T], id: str) -> T | None: ...
    def list(self, cls: type[T], **filters) -> list[T]: ...
    def add_message(self, msg: Message) -> None: ...
    def list_messages(self, session_id: str) -> list[Message]: ...
    def reset(self) -> None: ...
    def is_empty(self) -> bool: ...


class MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._data: dict[str, dict[str, dict]] = {c: {} for c in COLLECTIONS.values()}
            self._messages: dict[str, list[dict]] = {}

    def put(self, obj: BaseModel) -> None:
        with self._lock:
            self._data[COLLECTIONS[type(obj)]][obj.id] = obj.model_dump()

    def get(self, cls: type[T], id: str) -> T | None:
        raw = self._data[COLLECTIONS[cls]].get(id)
        return cls.model_validate(raw) if raw else None

    def list(self, cls: type[T], **filters) -> list[T]:
        rows = list(self._data[COLLECTIONS[cls]].values())
        rows = [r for r in rows if all(r.get(k) == v for k, v in filters.items())]
        return [cls.model_validate(r) for r in rows]

    def add_message(self, msg: Message) -> None:
        with self._lock:
            self._messages.setdefault(msg.session_id, []).append(msg.model_dump())

    def list_messages(self, session_id: str) -> list[Message]:
        rows = sorted(self._messages.get(session_id, []), key=lambda r: r["created_at"])
        return [Message.model_validate(r) for r in rows]

    def is_empty(self) -> bool:
        return not self._data["orgs"]


class FirestoreStore:
    def __init__(self, project: str, database: str = "(default)"):
        from google.cloud import firestore

        self._db = firestore.Client(project=project or None, database=database)

    def put(self, obj: BaseModel) -> None:
        self._db.collection(COLLECTIONS[type(obj)]).document(obj.id).set(obj.model_dump(mode="json"))

    def get(self, cls: type[T], id: str) -> T | None:
        snap = self._db.collection(COLLECTIONS[cls]).document(id).get()
        return cls.model_validate(snap.to_dict()) if snap.exists else None

    def list(self, cls: type[T], **filters) -> list[T]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self._db.collection(COLLECTIONS[cls])
        for k, v in filters.items():
            q = q.where(filter=FieldFilter(k, "==", v))
        return [cls.model_validate(s.to_dict()) for s in q.stream()]

    def _messages(self, session_id: str):
        return self._db.collection("sessions").document(session_id).collection("messages")

    def add_message(self, msg: Message) -> None:
        self._messages(msg.session_id).document(msg.id).set(msg.model_dump(mode="json"))

    def list_messages(self, session_id: str) -> list[Message]:
        rows = [Message.model_validate(s.to_dict()) for s in self._messages(session_id).stream()]
        return sorted(rows, key=lambda m: m.created_at)

    def reset(self) -> None:
        for name in COLLECTIONS.values():
            self._db.recursive_delete(self._db.collection(name))  # includes sessions/*/messages

    def is_empty(self) -> bool:
        return not list(self._db.collection("orgs").limit(1).stream())


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        s = get_settings()
        _store = FirestoreStore(s.project, s.firestore_database) if s.store == "firestore" else MemoryStore()
    return _store
