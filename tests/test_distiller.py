from app.distiller import distill
from app.models import Message, Playbook, Session
from app.seed import ORG_ID

VAULT_RESOLVED = {"ses_seed_00", "ses_seed_01", "ses_seed_02"}
VAULT_UNRESOLVED = "ses_seed_03"
HR_STIPEND = {"ses_seed_08", "ses_seed_09", "ses_seed_10"}


def _playbooks(store, kind):
    return store.list(Playbook, org_id=ORG_ID, agent_kind=kind)


def test_distill_creates_one_dev_and_one_hr_playbook(fresh_store):
    report = distill(ORG_ID)
    assert report.playbooks_created == 2
    [dev] = _playbooks(fresh_store, "dev")
    [hr] = _playbooks(fresh_store, "hr")
    assert set(dev.source_session_ids) == VAULT_RESOLVED
    assert set(hr.source_session_ids) == HR_STIPEND
    assert len(hr.source_employee_ids) == 3


def test_unresolved_session_never_contributes_steps(fresh_store):
    distill(ORG_ID)
    [dev] = _playbooks(fresh_store, "dev")
    assert VAULT_UNRESOLVED not in dev.source_session_ids
    assert dev.related_unresolved_session_ids == [VAULT_UNRESOLVED]
    assert not any("SSH" in step for step in dev.steps)


def test_unresolved_alone_cannot_make_playbook(fresh_store):
    # one resolved + one unresolved session from different employees on a new issue -> no playbook
    for sid, emp, last in [("ses_x1", "e_priya", "Ran `acme db migrate --unlock`, that worked."),
                           ("ses_x2", "e_tom", "Still broken, giving up.")]:
        s = Session(id=sid, org_id=ORG_ID, employee_id=emp, agent_id="a_dev", agent_kind="dev")
        fresh_store.add_message(Message(session_id=sid, role="user", content="acme db migrate hangs on lock table schema_migrations"))
        fresh_store.add_message(Message(session_id=sid, role="assistant", content="Check for stuck migration locks."))
        fresh_store.add_message(Message(session_id=sid, role="user", content=last))
        fresh_store.put(s)
    report = distill(ORG_ID, "dev")
    cluster = next(c for c in report.clusters if "ses_x1" in c.session_ids)
    assert cluster.outcome == "skipped"
    assert len(_playbooks(fresh_store, "dev")) == 1


def test_single_employee_repeats_are_excluded(fresh_store):
    report = distill(ORG_ID)
    docker = next(c for c in report.clusters if "ses_seed_04" in c.session_ids)
    assert docker.employee_ids == ["e_marcus"]
    assert docker.outcome == "skipped"


def test_rerun_is_unchanged_then_new_evidence_updates(fresh_store):
    distill(ORG_ID)
    again = distill(ORG_ID)
    assert again.sessions_summarized == 0
    assert again.playbooks_created == again.playbooks_updated == 0

    sid = "ses_new"
    fresh_store.add_message(Message(session_id=sid, role="user", content="acme deploy staging fails: vault permission denied ACME_VAULT_TOKEN expired"))
    fresh_store.add_message(Message(session_id=sid, role="assistant", content="Try refreshing the token."))
    fresh_store.add_message(Message(session_id=sid, role="user", content="acme auth refresh --vault on VPN fixed it."))
    fresh_store.put(Session(id=sid, org_id=ORG_ID, employee_id="e_dana", agent_id="a_dev", agent_kind="dev"))

    report = distill(ORG_ID, "dev")
    assert report.playbooks_updated == 1
    [dev] = _playbooks(fresh_store, "dev")
    assert dev.version == 2
    assert sid in dev.source_session_ids
