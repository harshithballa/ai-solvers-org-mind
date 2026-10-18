"""scripts/record_prep.py --dry-run: the actions it prints for recording and --finish, without touching anything."""
import subprocess
import sys

from scripts import record_prep


def dry_run(monkeypatch, capsys, *flags):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["record_prep", "--admin-token-file", "/nonexistent", "--dry-run", *flags])
    assert record_prep.main() == 0   # the token file is never read and no step runs
    assert calls == []
    return capsys.readouterr().out


def test_recording_plan_is_a_full_knowledge_reset(monkeypatch, capsys):
    out = dry_run(monkeypatch, capsys)
    assert f"target: {record_prep.DEFAULT_URL}" in out and "mode: recording" in out
    assert "delete EVERY document for all employees" in out and "purge_test_docs --all" in out
    assert "delete every share_events document" in out and "0 skills, 0 playbooks" in out
    assert f"upload {record_prep.DOC_TITLE} as Dana (private, not shared)" in out and "test-contact" not in out
    assert "probe: Liam" in out and "scripts.clear_traces" in out and "dry run: nothing was changed" in out


def test_live_docs_stages_no_document(monkeypatch, capsys):
    out = dry_run(monkeypatch, capsys, "--live-docs")
    assert "no document staged (--live-docs)" in out and "upload acme" not in out


def test_finish_plan_keeps_skill_and_playbooks(monkeypatch, capsys):
    out = dry_run(monkeypatch, capsys, "--finish")
    assert "mode: finish" in out and "assert 2 playbooks, exactly 1 skill for Dana" in out
    assert "scripts.demo_prep --yes --clear-share-events --reset-usage" in out and "--no-skill" not in out
    assert "--all" not in out and "GET the main URL" in out
