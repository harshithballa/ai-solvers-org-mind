"""Cost caps for the public demo: global per-UTC-day counters plus best-effort per-IP hourly limits.

- Counters live in the store (`usage_counters`, doc id "{UTC date}-{bucket}", atomic increment).
- Every real Gemini call is counted inside llm.py; multi-call operations reserve an estimate up front so a
  request is refused with 429 before it starts rather than dying half way.
- Requests with the valid admin token are neither counted nor limited. ORGMIND_LIMITS=off disables everything.
- Fail open: if the counter store errors, the request is allowed and a warning is logged.
"""
from __future__ import annotations

import contextvars
import hmac
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import get_settings

log = logging.getLogger("orgmind")

RESETS = "00:00 UTC"
MESSAGES = {
    "gemini_calls": "The demo has reached today's AI usage limit.",
    "chat_messages": "The demo has reached today's chat limit.",
    "distill": "Distill has reached today's limit.",
    "skills_extract": "Learning skills has reached today's limit.",
    "share_scans": "Sharing has reached today's limit.",
    "compare": "Compare has reached today's limit.",
    "rag_uploads": "Document uploads have reached today's limit.",
    "sessions_created": "New chats have reached today's limit.",
}
# Conservative Gemini-call estimates for multi-call operations (generate + embedding calls).
ESTIMATE = {"skills_extract": 4, "compare": 4, "share_scans": 2}


def distill_estimate(n_sessions: int) -> int:
    """Upper bound for one distill: a summary + an embedding per session, plus compile/embed per playbook."""
    return 2 * n_sessions + 12


class LimitExceeded(Exception):
    def __init__(self, bucket: str, per_ip: bool = False):
        self.bucket = bucket
        base = MESSAGES.get(bucket, "The demo has reached a usage limit.")
        if per_ip:
            self.message = f"{base.replace('today', 'this hour')} Please try again in a little while."
            self.resets = "within the hour"
        else:
            self.message = f"{base} It resets at {RESETS}."
            self.resets = RESETS
        super().__init__(self.message)

    def payload(self) -> dict:
        return {"detail": self.message, "limit": self.bucket, "resets": self.resets}


@dataclass
class _Ctx:
    bypass: bool = False
    ip: str = ""


_ctx: contextvars.ContextVar[_Ctx] = contextvars.ContextVar("orgmind_limits", default=_Ctx())


def utcnow() -> datetime:  # patched in tests
    return datetime.now(timezone.utc)


def day_key(bucket: str) -> str:
    return f"{utcnow():%Y-%m-%d}-{bucket}"


def client_ip(headers: dict, peer: str | None) -> str:
    # Best effort, NOT a security boundary: Cloud Run's front end appends the real client address as the
    # right-most X-Forwarded-For entry; anything to its left is client-supplied and can be spoofed.
    xff = headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() if xff else (peer or "")


def is_admin(token: str) -> bool:
    expected = get_settings().admin_token
    return bool(expected) and bool(token) and hmac.compare_digest(token, expected)


def begin_request(headers: dict, peer: str | None):
    """Called by middleware for every API request; returns a token for end_request."""
    return _ctx.set(_Ctx(bypass=is_admin(headers.get("x-admin-token", "")), ip=client_ip(headers, peer)))


def end_request(token) -> None:
    _ctx.reset(token)


def enabled() -> bool:
    return get_settings().limits != "off" and not _ctx.get().bypass


def _limit(bucket: str) -> int:
    return get_settings().daily_limits.get(bucket, 10**9)


def _store():
    from app.store import get_store
    return get_store()


def hit(bucket: str, n: int = 1) -> None:
    """Count n units against today's global bucket; raise LimitExceeded if that goes over the limit."""
    if not enabled():
        return
    key = day_key(bucket)
    try:
        new = _store().incr_counter(key, n)
    except Exception as e:
        log.warning("usage counter %s unavailable, allowing request: %s", key, e)
        return
    if new > _limit(bucket):
        try:
            _store().incr_counter(key, -n)  # don't let refused requests eat into tomorrow's view of today
        except Exception:
            pass
        raise LimitExceeded(bucket)


def reserve_gemini(estimate: int) -> None:
    """Refuse up front if fewer than `estimate` Gemini calls remain today (nothing is consumed here)."""
    if not enabled():
        return
    try:
        used = _store().get_counter(day_key("gemini_calls"))
    except Exception as e:
        log.warning("usage counter unavailable, allowing request: %s", e)
        return
    if used + estimate > _limit("gemini_calls"):
        raise LimitExceeded("gemini_calls")


# ---- per-IP sliding window (in-memory, per instance) ----

_ip_lock = threading.Lock()
_ip_hits: dict[tuple[str, str], deque] = {}
WINDOW_S = 3600


def ip_hit(bucket: str, now: float | None = None) -> None:
    if not enabled():
        return
    limit = get_settings().ip_limits.get(bucket)
    ip = _ctx.get().ip
    if not limit or not ip:
        return
    now = time.time() if now is None else now
    with _ip_lock:
        q = _ip_hits.setdefault((bucket, ip), deque())
        while q and q[0] <= now - WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            raise LimitExceeded(bucket, per_ip=True)
        q.append(now)


def reset_ip_limits() -> None:
    with _ip_lock:
        _ip_hits.clear()


def usage() -> dict:
    s = get_settings()
    rows = []
    for bucket, limit in s.daily_limits.items():
        try:
            count = _store().get_counter(day_key(bucket))
        except Exception:
            count = None
        rows.append({"bucket": bucket, "count": count, "limit": limit})
    return {"date": f"{utcnow():%Y-%m-%d}", "resets": RESETS, "limits_enabled": s.limits != "off",
            "ip_limits_per_hour": s.ip_limits, "buckets": rows}
