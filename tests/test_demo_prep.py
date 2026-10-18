"""scripts/demo_prep.py dry run: the plan it prints for the default run and for --no-skill / --no-distill."""
import sys

import pytest

from scripts import demo_prep


def dry_run(monkeypatch, capsys, *flags):
    monkeypatch.setattr(sys, "argv", ["demo_prep", "--base-url", "http://x", "--admin-token-file", "/nonexistent", *flags])
    assert demo_prep.main() == 0   # no --yes: prints the steps, never reads the token or calls the server
    return capsys.readouterr().out


def test_default_plan_distills_and_shares_the_skill(monkeypatch, capsys):
    out = dry_run(monkeypatch, capsys)
    assert "distill sessions into playbooks" in out and "approve the BigQuery skill" in out
    assert "share the document and the skill" in out and "dry run" in out


@pytest.mark.parametrize("flags,skipped,kept", [
    (["--no-skill"], ["approve the BigQuery skill"], ["distill sessions into playbooks", "check no employee has a skill"]),
    (["--no-distill"], ["distill sessions into playbooks"], ["check there are no playbooks", "approve the BigQuery skill"]),
    (["--no-skill", "--no-distill"], ["approve the BigQuery skill", "distill sessions into playbooks"],
     ["check no employee has a skill", "check there are no playbooks", f"upload {demo_prep.DOC_TITLE}.txt"]),
])
def test_live_recording_flags(monkeypatch, capsys, flags, skipped, kept):
    out = dry_run(monkeypatch, capsys, *flags)
    assert not any(s in out for s in skipped)
    assert all(k in out for k in kept)


def test_clear_share_events_is_planned_only_with_the_flag(monkeypatch, capsys):
    assert "share_events" not in dry_run(monkeypatch, capsys)
    out = dry_run(monkeypatch, capsys, "--clear-share-events", "--no-skill", "--no-distill")
    assert "delete every share_events document" in out and "dry run" in out


def test_url_and_token_file_default_to_the_live_service(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["demo_prep"])
    assert demo_prep.main() == 0   # dry run: the token file is not read
    assert f"target: {demo_prep.DEFAULT_URL}" in capsys.readouterr().out
    assert demo_prep.DEFAULT_URL == "https://orgmind-207900876373.us-central1.run.app"
    assert demo_prep.DEFAULT_TOKEN_FILE == "~/.config/orgmind/admin_token"
