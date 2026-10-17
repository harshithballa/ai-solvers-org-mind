"""PII / secret guardrail for sharing with the org.

Layer 1: deterministic patterns. Layer 2: one Gemini structured-output check (temperature 0) that tells org-wide
facts from personal values. A share is published only if BOTH layers are clean; if the Gemini check fails the scan
FAILS CLOSED ("couldn't verify, try again"). Patterns alone are never enough to publish.

Raw matched values never leave this module: findings expose a category, a masked preview and a reason. The raw
value is kept on the Finding object only so redaction can replace it, and is excluded from every serialization.
There is no human review step (planned).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from app.limits import LimitExceeded
from app.llm import get_llm
from app.models import GuardrailFindingOut, GuardrailOut
from app.prompts import GUARDRAIL_SYSTEM
from app.tracing import set_attrs, span

log = logging.getLogger("orgmind")

REDACTION_MARKER = re.compile(r"\[REDACTED:[A-Z_]+\]")
COULDNT_VERIFY = "Couldn't verify this content right now, so it was not shared. Please try again."
LLM_CATEGORIES = {"PERSONAL_CONTACT", "PERSONAL_ID", "CREDENTIAL", "PERSONAL_PROJECT", "HOME_ADDRESS", "HEALTH",
                  "FINANCIAL", "OTHER_PERSONAL"}


# ---------------- layer 1: patterns ----------------

def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _phone_ok(m: str) -> bool:
    n = len(_digits(m))
    return 8 <= n <= 15


def _card_ok(m: str) -> bool:
    d = _digits(m)
    return 13 <= len(d) <= 19 and luhn_ok(d)


def _mixed_b64(m: str) -> bool:
    return bool(re.search(r"\d", m) and re.search(r"[a-z]", m) and re.search(r"[A-Z]", m))


# (category, pattern, validator, human label). Order matters for overlaps: earlier = more specific.
DETECTORS = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"), None,
     "Private key blocks"),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), None, "JSON Web Tokens"),
    ("BEARER_TOKEN", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), None, "Bearer tokens"),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None, "AWS access key IDs"),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), None, "Email addresses"),
    ("CARD_NUMBER", re.compile(r"(?<![\w.-])\d(?:[ -]?\d){12,18}(?![\w:-]|\.\d)"), _card_ok, "Card numbers (13-19 digits, Luhn)"),
    ("AADHAAR", re.compile(r"(?<![\d-])[2-9]\d{3}[ -]\d{4}[ -]\d{4}(?![ -]?\d)"), None, "Aadhaar-style numbers (4-4-4)"),
    ("SSN", re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])"), None, "US SSN-style numbers"),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), None, "PAN numbers (AAAAA9999A)"),
    ("PHONE", re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,5}){1,4}(?!\d)"), _phone_ok,
     "International phone numbers"),
    ("PHONE", re.compile(r"(?<![\w+-])(?:0|91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\w-])"), _phone_ok, "Indian 10-digit mobile numbers"),
    ("SECRET_STRING", re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])"), None, "Long hex strings (32+)"),
    ("SECRET_STRING", re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/=_-])"), _mixed_b64,
     "Long base64-like strings (32+)"),
]


def detector_list() -> list[dict]:
    seen, out = set(), []
    for cat, _, _, label in DETECTORS:
        if label not in seen:
            seen.add(label)
            out.append({"category": cat, "label": label, "layer": "pattern"})
    out.append({"category": "PERSONAL_* / CREDENTIAL / ...", "label": "Gemini check: personal vs org-wide values",
                "layer": "llm"})
    return out


# ---------------- masking ----------------

def mask(value: str) -> str:
    """Masked preview: enough to recognise the kind of value, never the value. e.g. 'd***@g***.com'."""
    v = value.strip()
    m = re.fullmatch(r"([^@\s])[^@\s]*@([A-Za-z0-9])[^\s]*?\.([A-Za-z]{2,})", v)
    if m:
        return f"{m.group(1)}***@{m.group(2)}***.{m.group(3)}"
    if v.startswith("-----BEGIN"):
        return "-----BEGIN *** PRIVATE KEY-----"
    digits = _digits(v)
    if len(digits) >= 8 and len(digits) >= len(re.sub(r"[\s+()-]", "", v)) * 0.8:
        return f"***{digits[-2:]}"
    if len(v) <= 4:
        return "***"
    return f"{v[0]}***{v[-1]}"


# ---------------- results ----------------

@dataclass
class Finding:
    category: str
    preview: str
    reason: str
    source: Literal["pattern", "llm"]
    raw: str = field(repr=False, default="")   # internal only (redaction); never serialized

    def public(self) -> dict:
        return {"category": self.category, "preview": self.preview, "reason": self.reason, "source": self.source}


@dataclass
class ScanResult:
    verdict: Literal["clean", "blocked", "error"]
    findings: list[Finding]
    message: str = ""

    @property
    def categories(self) -> list[str]:
        return sorted({f.category for f in self.findings})

    def public(self) -> dict:
        return {"verdict": self.verdict, "message": self.message, "findings": [f.public() for f in self.findings]}


def detect_patterns(text: str) -> list[Finding]:
    spans: list[tuple[int, int, str]] = []
    for cat, rx, ok, _ in DETECTORS:
        for m in rx.finditer(text):
            if ok and not ok(m.group(0)):
                continue
            s, e = m.span()
            if any(s >= a and e <= b for a, b, _ in spans):   # inside a more specific earlier match
                continue
            spans = [(a, b, c) for a, b, c in spans if not (a >= s and b <= e)]
            spans.append((s, e, cat))
    out, seen = [], set()
    for s, e, cat in sorted(spans):
        raw = text[s:e]
        if (cat, raw) in seen:
            continue
        seen.add((cat, raw))
        out.append(Finding(cat, mask(raw), f"Matches the {cat.replace('_', ' ').lower()} pattern.", "pattern", raw))
    return out


def _sanitize_reason(reason: str, raws: list[str]) -> str:
    reason = reason.strip()[:200]
    for r in raws:
        if r and len(r) >= 3:
            reason = re.sub(re.escape(r), mask(r), reason, flags=re.I)
    for f in detect_patterns(reason):          # any value the model echoed despite instructions
        reason = reason.replace(f.raw, f.preview)
    return reason


def _fake_check(text: str) -> GuardrailOut:
    """Deterministic stand-in for tests/offline: flags personal sandbox project IDs only."""
    return GuardrailOut(findings=[
        GuardrailFindingOut(category="PERSONAL_PROJECT", quote=m.group(0), reason="Looks like one person's sandbox project.")
        for m in re.finditer(r"\bacme-sbx-[a-z0-9-]+", text)])


def scan(text: str, item_type: str) -> ScanResult:
    t0 = time.perf_counter()
    with span("guardrail.scan", **{"orgmind.item_type": item_type, "orgmind.chars": len(text)}) as sp:
        findings = detect_patterns(text)
        try:
            out = get_llm().generate_json(GUARDRAIL_SYSTEM, f"Content type: {item_type}\n<<<\n{text}\n>>>",
                                          GuardrailOut, fake=lambda: _fake_check(text))
        except LimitExceeded:
            raise
        except Exception as e:   # FAIL CLOSED: never publish what we couldn't verify
            log.warning("guardrail llm check failed, blocking share: %s", type(e).__name__)
            set_attrs(sp, **{"orgmind.guardrail.verdict": "error", "orgmind.guardrail.pattern_findings": len(findings)})
            return ScanResult("error", findings, COULDNT_VERIFY)
        pattern_raws = {f.raw.lower() for f in findings}
        llm_raws = []
        for f in out.findings:
            quote = f.quote.strip()
            if not quote or quote.lower() in pattern_raws or any(quote.lower() in r for r in pattern_raws):
                continue
            if not REDACTION_MARKER.sub("", quote).strip(" ,.;:()-"):   # only [REDACTED:...] markers: already removed
                continue
            cat = f.category.strip().upper().replace(" ", "_")
            cat = cat if cat in LLM_CATEGORIES else "OTHER_PERSONAL"
            llm_raws.append(quote)
            findings.append(Finding(cat, mask(quote), "", "llm", quote))
            findings[-1].reason = _sanitize_reason(f.reason, [quote])
        result = ScanResult("blocked" if findings else "clean", findings,
                            "" if not findings else "Personal or secret values found, so it was not shared.")
        latency = round((time.perf_counter() - t0) * 1000, 1)
        set_attrs(sp, **{"orgmind.guardrail.verdict": result.verdict, "orgmind.guardrail.findings": len(findings),
                         "orgmind.guardrail.categories": ",".join(result.categories) or None,
                         "orgmind.latency_ms": latency})
    log.info("guardrail scan %s: %s %s %.0fms", item_type, result.verdict, result.categories, latency)
    return result


def redact(text: str, findings: list[Finding]) -> str:
    """Replace every finding's raw value with [REDACTED:<CATEGORY>] (longest first so overlaps resolve)."""
    for f in sorted(findings, key=lambda f: -len(f.raw)):
        if f.raw:
            text = re.sub(re.escape(f.raw), f"[REDACTED:{f.category}]", text, flags=re.I)
    return text
