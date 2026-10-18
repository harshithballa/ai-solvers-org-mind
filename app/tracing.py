"""Observability: optional OpenTelemetry -> Cloud Trace, JSON logs correlated with the trace, and an
in-app record of every LLM call (GET /api/traces).

Design rules: tracing is optional (ORGMIND_TRACING=cloudtrace|off), never raises into request code,
and everything is flushed before the response is returned (Cloud Run throttles CPU afterwards).
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from opentelemetry import trace

from app.config import get_settings
from app.models import LLMCall, new_id, now

log = logging.getLogger("orgmind")
tracer = trace.get_tracer("orgmind")
EXCERPT_CHARS = 600

_provider = None  # SDK TracerProvider when ORGMIND_TRACING=cloudtrace


@dataclass
class RequestTrace:
    """Per-request context shared (by reference) with worker threads."""
    request: str
    trace_id: str = ""
    span_id: str = ""
    calls: list[LLMCall] = field(default_factory=list)


_current: contextvars.ContextVar[RequestTrace | None] = contextvars.ContextVar("orgmind_request", default=None)


# ---------------- setup ----------------

def init() -> None:
    _init_logging()
    global _provider
    s = get_settings()
    if s.tracing != "cloudtrace" or _provider is not None:
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON

        provider = TracerProvider(sampler=ALWAYS_ON, resource=Resource.create({
            "service.name": os.getenv("K_SERVICE", "orgmind"),
            "service.version": os.getenv("K_REVISION", "local"),
            "gcp.project_id": s.project}))
        provider.add_span_processor(BatchSpanProcessor(_otlp_exporter()))
        trace.set_tracer_provider(provider)
        _provider = provider
        global tracer
        tracer = trace.get_tracer("orgmind")
        log.info("tracing enabled", extra={"json_fields": {"exporter": "otlp:telemetry.googleapis.com"}})
    except Exception as e:  # tracing must never take the app down
        log.warning("tracing disabled: %s", e)


def _otlp_exporter():
    """OTLP/gRPC to the Google Cloud Telemetry API (the recommended Cloud Trace ingestion path)."""
    import google.auth
    import google.auth.transport.requests
    import grpc
    from google.auth.transport.grpc import AuthMetadataPlugin
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    plugin = AuthMetadataPlugin(credentials=creds, request=google.auth.transport.requests.Request())
    channel_creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(),
                                                       grpc.metadata_call_credentials(plugin))
    return OTLPSpanExporter(endpoint="telemetry.googleapis.com:443", credentials=channel_creds, timeout=5)


_flush_thread: threading.Thread | None = None


def _force_flush() -> None:
    try:
        _provider.force_flush()
    except Exception as e:
        log.warning("trace flush failed: %s", e)


def flush(timeout_ms: int = 2000) -> None:
    """Export queued spans, holding the response for at most timeout_ms.

    The SDK's BatchSpanProcessor.force_flush ignores its timeout and exports synchronously (the OTLP exporter
    retries a slow Telemetry API for ~10s), so it runs on a worker thread here. If an earlier flush is still
    running, no second one is started: queued spans go out with that export or the next one."""
    global _flush_thread
    if _provider is None:
        return
    if _flush_thread is None or not _flush_thread.is_alive():
        _flush_thread = threading.Thread(target=_force_flush, name="trace-flush", daemon=True)
        _flush_thread.start()
    _flush_thread.join(timeout_ms / 1000)
    if _flush_thread.is_alive():
        log.warning("trace export still running after %dms; not holding the response", timeout_ms)


# ---------------- request scope ----------------

_PROPAGATOR = None


def _extract_parent(headers: dict):
    """Join the Cloud Run request trace (traceparent or X-Cloud-Trace-Context) when present."""
    global _PROPAGATOR
    try:
        if _PROPAGATOR is None:
            from opentelemetry.propagate import composite
            from opentelemetry.propagators.cloud_trace_propagator import CloudTraceFormatPropagator
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
            _PROPAGATOR = composite.CompositePropagator([TraceContextTextMapPropagator(), CloudTraceFormatPropagator()])
        return _PROPAGATOR.extract(headers)
    except Exception:
        return None


def _header_trace_id(headers: dict) -> str:
    m = re.match(r"([0-9a-f]{32})/", headers.get("x-cloud-trace-context", ""))
    if m:
        return m.group(1)
    m = re.match(r"[0-9a-f]{2}-([0-9a-f]{32})-", headers.get("traceparent", ""))
    return m.group(1) if m else ""


@contextmanager
def request_scope(request: str, headers: dict):
    """Root span + in-app call buffer for one chat/distill/compare request."""
    rt = RequestTrace(request=request)
    token = _current.set(rt)
    try:
        with _span(f"orgmind.{request}", context=_extract_parent(headers), attributes={"orgmind.request": request}) as span:
            ctx = span.get_span_context() if span is not None else None
            if ctx is not None and ctx.is_valid:
                rt.trace_id, rt.span_id = f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
            else:  # tracing off: reuse Cloud Run's trace id for log correlation, or a local id for grouping
                rt.trace_id = _header_trace_id(headers) or os.urandom(16).hex()
            yield rt
    finally:
        _current.reset(token)


def current() -> RequestTrace | None:
    return _current.get()


def wrap(fn):
    """Run fn in a copy of the caller's context (span + request buffer) inside a worker thread."""
    ctx = contextvars.copy_context()
    return lambda *a, **k: ctx.copy().run(fn, *a, **k)


@contextmanager
def _span(name: str, context=None, attributes: dict | None = None):
    try:
        cm = tracer.start_as_current_span(name, context=context, attributes=attributes or {})
        span = cm.__enter__()
    except Exception:
        yield None
        return
    try:
        yield span
    except BaseException as e:
        try:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)[:200]))
        except Exception:
            pass
        cm.__exit__(type(e), e, e.__traceback__)
        raise
    else:
        cm.__exit__(None, None, None)


def span(name: str, **attributes):
    """Child span helper for pipeline code (retrieval, Firestore writes, distill phases)."""
    return _span(name, attributes={k: v for k, v in attributes.items() if v is not None})


def set_attrs(span_obj, **attributes) -> None:
    if span_obj is None:
        return
    try:
        for k, v in attributes.items():
            if v is not None:
                span_obj.set_attribute(k, v)
    except Exception:
        pass


# ---------------- LLM call recording ----------------

def _excerpt(text) -> str:
    text = text if isinstance(text, str) else json.dumps(text, default=str)
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + "…"


@contextmanager
def llm_call(operation: str, model: str, prompt):
    """Span + in-app record + JSON log for one Gemini generate/embed call.

    Yields a dict the caller fills with response/usage info; recording problems are swallowed.
    """
    info: dict = {}
    t0 = time.perf_counter()
    err = None
    with _span(f"gemini.{operation}", attributes={"gen_ai.system": "vertex_ai", "gen_ai.request.model": model,
                                                   "orgmind.operation": operation}) as sp:
        try:
            yield info
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:300]}"
            raise
        finally:
            latency = (time.perf_counter() - t0) * 1000
            set_attrs(sp, **{"gen_ai.usage.input_tokens": info.get("input_tokens"),
                             "gen_ai.usage.output_tokens": info.get("output_tokens"),
                             "orgmind.playbook_ids": ",".join(info.get("playbook_ids") or []) or None,
                             "orgmind.latency_ms": round(latency, 1)})
            _record(operation, model, prompt, info, latency, err, sp)


def _record(operation, model, prompt, info, latency, err, sp) -> None:
    try:
        rt = current()
        ctx = sp.get_span_context() if sp is not None else None
        call = LLMCall(
            id=new_id("call"), ts=now(), request=rt.request if rt else "other",
            trace_id=rt.trace_id if rt else "", span_id=f"{ctx.span_id:016x}" if ctx and ctx.is_valid else "",
            operation=operation, model=model, latency_ms=round(latency, 1),
            input_tokens=info.get("input_tokens"), output_tokens=info.get("output_tokens"),
            playbook_ids=info.get("playbook_ids") or [], error=err,
            prompt_excerpt=_excerpt(prompt), response_excerpt=_excerpt(info.get("response", "")))
        if rt is not None:
            rt.calls.append(call)
        else:  # LLM call outside a traced request: store immediately
            _store_calls([call])
        log.info("llm call %s %.0fms", operation, latency, extra={"json_fields": {
            "operation": operation, "model": model, "latency_ms": round(latency, 1),
            "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
            "playbook_ids": call.playbook_ids, "error": err, "request": call.request}, "trace_id": call.trace_id,
            "span_id": call.span_id})
    except Exception as e:
        log.warning("llm call recording failed: %s", e)


def _store_calls(calls: list[LLMCall]) -> None:
    if not calls:
        return
    try:
        from app.store import get_store
        get_store().add_llm_calls(calls, cap=get_settings().trace_cap)
    except Exception as e:
        log.warning("storing llm calls failed: %s", e)


def finish(rt: RequestTrace, status: int, duration_ms: float) -> None:
    """Persist this request's LLM calls, log a summary, flush spans. Never raises."""
    try:
        _store_calls(rt.calls)
        log.info("%s request done in %.0fms (%d llm calls)", rt.request, duration_ms, len(rt.calls),
                 extra={"json_fields": {"request": rt.request, "status": status, "duration_ms": round(duration_ms, 1),
                                        "llm_calls": len(rt.calls)}, "trace_id": rt.trace_id, "span_id": rt.span_id})
    except Exception:
        pass
    flush()


# ---------------- structured logging ----------------

class _CloudJsonFormatter(logging.Formatter):
    """One JSON object per line; Cloud Logging maps severity/message and the trace correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        entry.update(getattr(record, "json_fields", {}) or {})
        trace_id = getattr(record, "trace_id", "") or (current().trace_id if current() else "")
        project = get_settings().project
        if trace_id and project:
            entry["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace_id}"
            if getattr(record, "span_id", ""):
                entry["logging.googleapis.com/spanId"] = record.span_id
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _init_logging() -> None:
    if getattr(log, "_orgmind_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    on_cloud_run = bool(os.getenv("K_SERVICE"))
    handler.setFormatter(_CloudJsonFormatter() if on_cloud_run else logging.Formatter("%(levelname)s orgmind: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    log._orgmind_configured = True
