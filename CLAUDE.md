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

## Distiller rules
- A playbook needs a cluster of similar sessions from **at least 2 distinct employees** with resolved sessions. Repeats by a single employee don't count.
- **Only resolved sessions contribute steps.** Unresolved sessions in a cluster are recorded as related but are never sent to the playbook compiler.
- If a cluster matches an existing playbook (shared source session or centroid similarity), the playbook is recompiled from the union of sources and `version` is bumped. There is no elaborate merge logic.
- Thresholds: `ORGMIND_CLUSTER_THRESHOLD` and `ORGMIND_RETRIEVAL_THRESHOLD`. Tune them with `scripts/eval_questions.py`.

## Security
- `POST /api/demo/reset` requires the header `X-Admin-Token` to equal `ORGMIND_ADMIN_TOKEN`. When the token is unset, reset is disabled.
- PII redaction is out of scope for V1 (planned for the next version).

## Conventions
- Python 3.12+, Pydantic v2. `app/models.py` is the single source of truth for data shapes.
- Every LLM call that returns data uses Gemini structured output (`response_mime_type="application/json"` + `response_schema`). Don't parse free text with regexes.
- Prompts live in `app/prompts.py`.
- Run locally: `uvicorn app.main:app --reload --port 8080`. Tests: `pytest` (fake LLM + memory store).
- Deploy: `gcloud run deploy orgmind --source . --region $GOOGLE_CLOUD_LOCATION`.
