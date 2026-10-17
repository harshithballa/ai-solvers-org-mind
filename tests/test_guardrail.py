import json

import pytest

from app import guardrail
from app.guardrail import detect_patterns, luhn_ok, mask, redact, scan

CASES = [
    ("mail dana.kim@gmail.com now", "EMAIL", "dana.kim@gmail.com"),
    ("call +91 98765 43210 today", "PHONE", "+91 98765 43210"),
    ("call +1 415-555-0123 today", "PHONE", "+1 415-555-0123"),
    ("mobile 9876543210 please", "PHONE", "9876543210"),
    ("card 4111 1111 1111 1111 ok", "CARD_NUMBER", "4111 1111 1111 1111"),
    ("paid with card 4111 1111 1111 1111.", "CARD_NUMBER", "4111 1111 1111 1111"),
    ("card 5500005555555559 ok", "CARD_NUMBER", "5500005555555559"),
    ("aadhaar 2345 6789 0123 here", "AADHAAR", "2345 6789 0123"),
    ("ssn 123-45-6789 here", "SSN", "123-45-6789"),
    ("pan ABCDE1234F here", "PAN", "ABCDE1234F"),
    ("key AKIAIOSFODNN7EXAMPLE here", "AWS_ACCESS_KEY", "AKIAIOSFODNN7EXAMPLE"),
    ("Authorization: Bearer abcDEF123456ghiJKL789 x", "BEARER_TOKEN", "Bearer abcDEF123456ghiJKL789"),
    ("t eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U x", "JWT", None),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "PRIVATE_KEY", None),
    ("hex 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b x", "SECRET_STRING", "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"),
    ("b64 c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldDEyMw== x", "SECRET_STRING", None),
]


@pytest.mark.parametrize("text,category,raw", CASES)
def test_each_detector(text, category, raw):
    found = detect_patterns(text)
    assert [f.category for f in found] == [category], found
    if raw:
        assert found[0].raw == raw


def test_org_facts_are_not_flagged_by_patterns():
    text = ("Post in #release-ops and tag @rohan-release. Query acme-analytics.events.orders, ticket REL-1234, "
            "INC-20261017, 02:30 UTC, keep 21 days, limit $500, datastore orgmind-rag-datastore_1792251536297, "
            "session ses_seed_14, doc doc_b1a63c0084, version 1.2.3, {project_id}, <your sandbox project>")
    assert detect_patterns(text) == []


def test_luhn():
    assert luhn_ok("4111111111111111") and luhn_ok("79927398713")
    assert not luhn_ok("4111111111111112")
    assert detect_patterns("order 4111 1111 1111 1112 x") == []   # fails Luhn


@pytest.mark.parametrize("text,category,raw", [c for c in CASES if c[2]])
def test_masking_never_leaks_raw_values(text, category, raw):
    m = mask(raw)
    assert m != raw and raw not in m
    core = raw.split("@")[0] if "@" in raw else raw
    assert not any(core[i:i + 5] in m for i in range(len(core) - 4)), m


def test_email_mask_shape():
    assert mask("dana.kim@gmail.com") == "d***@g***.com"


def test_scan_fails_closed_when_gemini_errors(monkeypatch):
    from app.llm import get_llm

    def boom(*a, **k):
        raise RuntimeError("vertex unavailable")
    monkeypatch.setattr(get_llm(), "generate_json", boom)
    res = scan("A perfectly clean note about #release-ops.", "document")
    assert res.verdict == "error" and "Couldn't verify" in res.message and res.findings == []


def test_scan_patterns_alone_never_publish(monkeypatch):
    """Even a pattern-clean text is only clean once the Gemini layer has run."""
    from app.llm import get_llm
    called = []
    real = get_llm().generate_json
    monkeypatch.setattr(get_llm(), "generate_json", lambda *a, **k: called.append(1) or real(*a, **k))
    assert scan("Freeze is Thursdays 4pm-6pm IST.", "document").verdict == "clean"
    assert called == [1]


def test_llm_findings_are_masked_and_reasons_sanitized(monkeypatch):
    from app.llm import get_llm
    from app.models import GuardrailFindingOut, GuardrailOut
    out = GuardrailOut(findings=[GuardrailFindingOut(category="personal_contact", quote="Dana Kim, 12 Baker Street",
                                                     reason="Dana Kim, 12 Baker Street is a home address; mail x@y.org")])
    real = get_llm().generate_json
    monkeypatch.setattr(get_llm(), "generate_json", lambda system, contents, schema, fake, think=False:
                        real(system, contents, schema, lambda: out, think))
    res = scan("Send it to Dana Kim, 12 Baker Street.", "document")
    blob = json.dumps(res.public())
    assert res.verdict == "blocked" and res.findings[0].category == "PERSONAL_CONTACT"
    assert "Baker Street" not in blob and "x@y.org" not in blob


def test_fake_check_flags_personal_sandbox_project():
    res = scan("Run bq with --project_id=acme-sbx-dkim-4821 then check acme-analytics.", "document")
    assert res.verdict == "blocked" and res.categories == ["PERSONAL_PROJECT"]
    assert "acme-sbx-dkim-4821" not in json.dumps(res.public())


def test_redact_replaces_every_finding():
    text = "Mail dana.kim@gmail.com, card 4111 1111 1111 1111, sandbox acme-sbx-dkim-4821."
    res = scan(text, "document")
    red = redact(text, res.findings)
    assert "[REDACTED:EMAIL]" in red and "[REDACTED:CARD_NUMBER]" in red and "[REDACTED:PERSONAL_PROJECT]" in red
    assert scan(red, "document").verdict == "clean"


def test_guardrail_calls_record_no_excerpts(fresh_store):
    from app.models import GuardrailOut  # noqa: F401
    scan("mail dana.kim@gmail.com", "document")
    call = fresh_store.list_llm_calls(5)[0]
    assert call.operation == "guardrail_scan"
    assert "dana" not in call.prompt_excerpt and "dana" not in call.response_excerpt
    fresh_store._llm_calls.clear()


def test_detector_list_for_admin():
    labels = [d["label"] for d in guardrail.detector_list()]
    assert any("Luhn" in l for l in labels) and any(d["layer"] == "llm" for d in guardrail.detector_list())


def test_llm_findings_on_redaction_markers_are_ignored(monkeypatch):
    from app.llm import get_llm
    from app.models import GuardrailFindingOut, GuardrailOut
    out = GuardrailOut(findings=[GuardrailFindingOut(category="PERSONAL_CONTACT", quote="[REDACTED:EMAIL]", reason="contact")])
    real = get_llm().generate_json
    monkeypatch.setattr(get_llm(), "generate_json", lambda system, contents, schema, fake, think=False:
                        real(system, contents, schema, lambda: out, think))
    assert scan("Reach the team at [REDACTED:EMAIL].", "document").verdict == "clean"
