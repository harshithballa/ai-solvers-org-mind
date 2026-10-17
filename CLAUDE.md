# OrgMind

Hackathon prototype (Google Cloud AI Builder Cup, theme: Future of Work & Enterprise Productivity). 2-day build: speed over polish, but the pipeline logic is real (no faked outputs).

## Product
Enterprise agent platform. Each team gets pre-bundled generic agents (Dev Agent, HR Agent). Employee chat sessions are siloed by default.
Differentiator: the **distiller** finds problems that repeat across *different employees'* sessions and compiles them into structured **playbooks** (title, trigger, steps, sources). At answer time agents retrieve relevant playbooks and cite them, instead of stuffing raw chat history into context.

Demo story: 2–3 synthetic employees hit the same problem → run distill → playbook appears → a new employee asks → agent answers noticeably better and cites the playbook (`/api/compare` side-by-side "without vs with playbooks").

Priorities: the Dev agent flow and `/api/compare` come first. The HR flow is a bonus.

## Hard constraints
- Google stack: Gemini on Vertex AI via the `google-genai` SDK (`genai.Client(vertexai=True, project=..., location=...)`), Firestore, Cloud Run.
- One Cloud Run service: FastAPI backend that also serves the static frontend (plain HTML/JS/CSS, no build step).
- Storage: Firestore when `ORGMIND_STORE=firestore`; in-memory otherwise, so it runs locally with zero setup. All storage goes through the `Store` interface in `app/store.py`; never call Firestore directly elsewhere.
- Synthetic data only: fictional company "Acme Corp". No real people, companies, or credentials.
- Config only via env vars (see `app/config.py` and `.env.example`).
- `ORGMIND_LLM=fake` gives a deterministic stub LLM/embedder for tests and offline UI work. The demo always runs against real Gemini.

## Knowledge types (keep distinct)
- **Playbook**: org-wide fix distilled from the same problem across 2+ employees' resolved sessions (`app/distiller.py`).
- **RAG doc**: raw uploaded source material searched at answer time (`app/rag.py` providers, `app/rag_docs.py` ownership/scope).
- **Skill**: reusable procedure ONE employee's agent learned in ONE conversation (`app/skills.py`); private draft until the owner approves.
- Prompt order when several are on: playbooks, skills, document excerpts. Every retrieval failure degrades to answering without it.

## Distiller rules
- A playbook needs a cluster of similar sessions from **at least 2 distinct employees** with resolved sessions. Repeats by a single employee don't count.
- **Only resolved sessions contribute steps.** Unresolved sessions in a cluster are recorded as related but are never sent to the playbook compiler.
- If a cluster matches an existing playbook (shared source session or centroid similarity), the playbook is recompiled from the union of sources and `version` is bumped. There is no elaborate merge logic.
- Playbooks must generalize: the writer classifies `org_wide_facts` (kept: commands, menu paths, policy limits, deadlines) vs `excluded_specifics` (one person's names, cost centers, amounts). A leaked specific triggers one recompile, then a placeholder replacement; org-wide facts are never scrubbed.
- Thresholds: `ORGMIND_CLUSTER_THRESHOLD` (0.88) and `ORGMIND_RETRIEVAL_THRESHOLD` (0.62). Gemini embeddings put unrelated support chats at ~0.8, so clustering is complete-linkage. Tune with `scripts/eval_questions.py` against real Gemini (fake mode fails it by design).

## UI
- Default landing is the User workspace `#/user` (one page: Viewing-as switcher, agent cards, chats, My knowledge). Admin is a header link to `#/admin/<tab>` (Knowledge/Compare, Traces, Roadmap, previews). Vanilla HTML/JS/CSS, no build step.
- Every label must be true: capability chips and toggles only for what the code supports (Documents only when `ORGMIND_RAG_PROVIDER` is on); chat toggles default OFF.
- Preview tabs (`static/previews.js`) must show the "Preview · planned" badge, keep every control disabled with a "Planned for …" tooltip, label sample rows "Sample", and make no network calls. Never show invented metrics.

## Observability
- `app/tracing.py`: `ORGMIND_TRACING=cloudtrace|off` (off locally and always off in tests). Spans go via OTLP/gRPC to `telemetry.googleapis.com` (the deprecated `CloudTraceSpanExporter` silently dropped spans).
- One root span per chat/distill/compare request (middleware); child spans for retrieval, every Gemini call (`llm.py` → `tracing.llm_call`) and Firestore writes. Worker threads must use `tracing.wrap(fn)` to keep the request context.
- Every LLM call is also recorded (capped `llm_calls`, separate from demo data) for `GET /api/traces`; JSON logs on Cloud Run carry `logging.googleapis.com/trace`. Recording/export must never raise into request code; flush happens before the response returns.

## Cost caps
- `app/limits.py`: daily global buckets + per-IP hourly limits; every real Gemini call counts in `llm.py`; multi-call
  operations reserve an estimate first. 429 payload `{detail, limit, resets}`. Admin token bypasses; fail open on
  counter errors; `ORGMIND_LIMITS=off` in tests and eval scripts. Wrap pipelines with limits, never change their logic.

## Security
- `POST /api/demo/reset` requires the header `X-Admin-Token` to equal `ORGMIND_ADMIN_TOKEN`. When the token is unset, reset is disabled.
- On Cloud Run the admin token comes from Secret Manager (`orgmind-admin-token`), not a plain env var. Never print it.
- Sharing to the org goes through `app/guardrail.py` (patterns + Gemini, fail closed). Findings are category + masked
  preview only, never raw values, in API responses, UI, logs, spans, `share_events` and `llm_calls` excerpts.
  Human review before publish is planned (not built). PII redaction of sessions before distill is deferred to V3.

## Conventions
- Python 3.12+, Pydantic v2. `app/models.py` is the single source of truth for data shapes.
- Every LLM call that returns data uses Gemini structured output (`response_mime_type="application/json"` + `response_schema`). Don't parse free text with regexes.
- Prompts live in `app/prompts.py`.
- Run locally: `uvicorn app.main:app --reload --port 8080`. Tests: `pytest` (fake LLM + memory store). Dev deps: `requirements-dev.txt`; the image installs only `requirements.txt`.
- Must stay green: `python -m pytest`; `scripts/skills_eval.py` (real Gemini); `scripts/rag_smoke.py --gemini` (real data store); `scripts/eval_questions.py` (12/12 + leak + Kafka checks, real Gemini); `scripts/demo_flow.py` (headless browser flow); `scripts/user_flow.py` (User workspace, real Vertex + Gemini); `scripts/ui_tabs_check.py` (every tab at desktop + phone width, preview rules); `scripts/firestore_persistence_check.py` (uses the `orgmind-test` database, never `(default)`).
- Firestore database is `ORGMIND_FIRESTORE_DATABASE` (default `(default)` = demo). Seeding happens on startup only when the store is empty.
- Deploy: see README "Deploy (Cloud Run)" (keeps `--set-secrets ORGMIND_ADMIN_TOKEN=orgmind-admin-token:latest` and `ORGMIND_TRACING=cloudtrace`).
