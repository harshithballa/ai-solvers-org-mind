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


def test_recording_plan(monkeypatch, capsys):
    out = dry_run(monkeypatch, capsys)
    assert f"target: {record_prep.DEFAULT_URL}" in out and "mode: recording" in out
    assert "purge test- documents" in out and "upload it as Dana" in out and "~/test-contact.txt" in out
    assert "--yes --clear-share-events --reset-usage --no-skill --no-distill" in out
    assert "scripts.clear_traces" in out and "dry run: nothing was changed" in out


def test_finish_plan_keeps_skill_and_playbooks(monkeypatch, capsys):
    out = dry_run(monkeypatch, capsys, "--finish")
    assert "mode: finish" in out and "assert 2 playbooks, exactly 1 skill for Dana" in out
    assert "--yes --clear-share-events --reset-usage" in out and "--no-skill" not in out and "--no-distill" not in out
    assert "test-contact" not in out and "GET the main URL" in out


def test_contact_file_has_synthetic_pii_for_the_guardrail():
    assert record_prep.EMAIL in record_prep.CONTACT_TEXT and record_prep.PHONE in record_prep.CONTACT_TEXT
    assert record_prep.CONTACT_TITLE.startswith("test-")   # so purge_test_docs removes it
