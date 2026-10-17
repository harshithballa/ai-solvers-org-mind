"""Store-level parity: the same operations against MemoryStore and (opt-in) a scratch Firestore database.

    ORGMIND_PARITY_DB=orgmind-test GOOGLE_CLOUD_PROJECT=<project> python -m pytest tests/test_store_parity.py

Firestore runs only when ORGMIND_PARITY_DB names a scratch database; "(default)" is always refused. The test may
wipe that database's collections (it calls reset()), so never point it at real data.
"""
import os
from datetime import timedelta

import pytest

from app.models import (LLMCall, Message, RagDoc, Session, ShareEvent, Skill, SkillParameter, new_id, now)
from app.store import FirestoreStore, MemoryStore

PARITY_DB = os.getenv("ORGMIND_PARITY_DB", "")
ORG = "parity"


def _stores():
    yield pytest.param("memory", id="memory")
    if PARITY_DB:
        yield pytest.param("firestore", id=f"firestore:{PARITY_DB}")


@pytest.fixture(params=list(_stores()))
def store(request):
    if request.param == "memory":
        return MemoryStore()
    assert PARITY_DB not in ("", "(default)", "default"), "refusing to run against the (default) database"
    st = FirestoreStore(os.getenv("GOOGLE_CLOUD_PROJECT", ""), PARITY_DB)
    assert st._db._database == PARITY_DB   # verify the database id before any write
    return st


def test_put_get_list_filters_delete(store):
    a = RagDoc(org_id=ORG, owner_employee_id="e_a", title="A", mime_type="text/plain", size=1, gcs_uri=f"fake://{new_id('x')}")
    b = RagDoc(org_id=ORG, owner_employee_id="e_b", title="B", mime_type="text/plain", size=1, scope="org",
               shared_by="e_b", shared_at=now(), gcs_uri=f"fake://{new_id('x')}")
    for d in (a, b):
        store.put(d)
    assert store.get(RagDoc, a.id).title == "A"
    assert {d.id for d in store.list(RagDoc, org_id=ORG, owner_employee_id="e_a")} == {a.id}
    assert {d.id for d in store.list(RagDoc, org_id=ORG, scope="org")} == {b.id}
    assert store.get(RagDoc, b.id).shared_by == "e_b"
    store.delete(RagDoc, a.id)
    assert store.get(RagDoc, a.id) is None


def test_skills_and_share_events(store):
    sk = Skill(org_id=ORG, owner_employee_id="e_a", agent_kind="dev", name="n", description="d", trigger="t",
               steps=["s1", "s2"], parameters=[SkillParameter(name="project_id", description="p")], evidence=[1, 3],
               source_session_id=f"ses_{new_id('p')}", embedding=[0.1] * 768)
    store.put(sk)
    got = store.list(Skill, source_session_id=sk.source_session_id)
    assert [s.id for s in got] == [sk.id] and got[0].parameters[0].name == "project_id" and len(got[0].embedding) == 768
    assert [s.id for s in store.list(Skill, org_id=ORG, owner_employee_id="e_a") if s.id == sk.id] == [sk.id]
    ev = ShareEvent(org_id=ORG, actor_employee_id="e_a", item_type="skill", item_id=sk.id, action="blocked",
                    verdict="blocked", finding_categories=["EMAIL"])
    store.put(ev)
    assert [e.finding_categories for e in store.list(ShareEvent, item_id=sk.id)] == [["EMAIL"]]
    assert any(e.id == ev.id for e in store.list(ShareEvent, org_id=ORG))


def test_messages_are_ordered(store):
    sid = new_id("ses")
    t0 = now()
    for i in (2, 0, 1):
        store.add_message(Message(session_id=sid, role="user", content=f"m{i}", created_at=t0 + timedelta(seconds=i),
                                  cited_playbook_ids=["pb"] if i == 1 else []))
    msgs = store.list_messages(sid)
    assert [m.content for m in msgs] == ["m0", "m1", "m2"] and msgs[1].cited_playbook_ids == ["pb"]


def test_llm_calls_cap_and_order(store):
    t0 = now()
    calls = [LLMCall(id=new_id("call"), ts=t0 + timedelta(seconds=i), request="chat", operation="answer", model="m",
                     latency_ms=1.0) for i in range(3)]
    store.add_llm_calls(calls, cap=500)
    newest = store.list_llm_calls(3)
    assert newest[0].id == calls[-1].id


def test_counters_increment(store):
    key = f"2099-01-01-{new_id('bucket')}"
    assert store.get_counter(key) == 0
    assert store.incr_counter(key) == 1
    assert store.incr_counter(key, 2) == 3
    assert store.incr_counter(key, -1) == 2
    assert store.get_counter(key) == 2


def test_reset_keeps_rag_docs_share_events_and_counters(store):
    doc = RagDoc(org_id=ORG, owner_employee_id="e_a", title="keep", mime_type="text/plain", size=1)
    ev = ShareEvent(org_id=ORG, actor_employee_id="e_a", item_type="doc", item_id=doc.id, action="share", verdict="clean")
    ses = Session(org_id=ORG, employee_id="e_a", agent_id="a_dev", agent_kind="dev")
    key = f"2099-01-02-{new_id('bucket')}"
    for o in (doc, ev, ses):
        store.put(o)
    store.incr_counter(key)
    store.reset()
    assert store.get(RagDoc, doc.id) is not None and store.get(ShareEvent, ev.id) is not None
    assert store.get(Session, ses.id) is None and store.get_counter(key) == 1
