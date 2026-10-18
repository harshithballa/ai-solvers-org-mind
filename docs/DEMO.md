# Demo guide

How to prepare the live service for a demo or a recording, and a walkthrough script. Back to the [README](../README.md).

## Seed data and reset

- On startup the app seeds Acme Corp **only if the store is empty**: 8 employees, Dev + HR agents and
  15 scripted sessions. Those include a recurring vault-token problem (3 resolved, 1 unresolved), a stipend
  problem raised by 3 employees, a problem one employee repeated, a recurring problem nobody solved (Kafka),
  some unrelated noise, and Dana's BigQuery session (`ses_seed_14`, one employee working out a reusable procedure:
  the skill-extraction demo; it forms no cluster and no playbook).
- Summaries, clusters and playbooks are always computed live by Gemini. Nothing is pre-baked.
- **Reset demo** (top-right button on Admin pages, or `curl -X POST -H "X-Admin-Token: $ORGMIND_ADMIN_TOKEN" <url>/api/demo/reset`)
  wipes sessions and playbooks and re-seeds.

## Preparing the demo state

`python -m scripts.demo_prep` prepares the demo state on a running server. It is a dry run (prints the plan) unless
`--yes` is passed. Default: reset, distill, upload and wait for `acme-bq-onboarding` as Dana, learn and approve the
BigQuery skill from her chat `ses_seed_14`, share the doc and the skill (guardrail scan), print the final state.
All requests carry the admin token, so they don't count against the cost caps.

| Flag | Effect |
|---|---|
| `--base-url` | Server to prepare; default `https://orgmind-207900876373.us-central1.run.app` |
| `--admin-token-file` | File holding the admin token; default `~/.config/orgmind/admin_token` (never printed) |
| `--no-skill` | No skill is extracted/approved/shared; checks no employee has a skill. `ses_seed_14` stays, so *Learn skills from this chat* can run live |
| `--no-distill` | No distill; checks there are no playbooks, so *Admin › Knowledge* shows its empty state and *Distill sessions* can run live |
| `--clear-share-events` | Deletes every `share_events` document (a reset keeps this audit log), so *Admin › Guardrails* starts empty; prints the count. Talks to Firestore directly (`--database`, default `(default)`) |
| `--reset-usage` | Deletes only today's (UTC) `usage_counters` documents, so every daily cap starts fresh; prints the deleted ids. Earlier days are kept. Same Firestore access and database check as `--clear-share-events` |

The shared `acme-bq-onboarding` document is prepared in every mode. For a live recording:
`python -m scripts.demo_prep --yes --no-skill --no-distill --clear-share-events --reset-usage`.

`python -m scripts.record_prep` does the whole recording setup on the main URL: purges `test-` docs, runs that
demo_prep command, uploads `~/test-contact.txt` (synthetic email + phone) as Dana and waits for Ready, clears traces
and checks the state. After recording, `python -m scripts.record_prep --finish` restores the judge-ready default state
(2 playbooks, Dana's shared skill and doc) and asserts it. `--dry-run` prints the actions only.

## Demo script (3–4 min)

Before each take: open **Admin** (header link), click **Reset demo** (top right, admin token). Start on **Admin › Knowledge** (Compare); the app itself opens on the User workspace.

1. **The silo (Admin › Knowledge).** Pick *Dev Agent* and click the first preset, *"My staging deploy is failing
   with ACME_VAULT_TOKEN expired."* The left answer is generic, and Gemini often invents an `acme vault login`
   command. The right answer says *"No playbook matched"*. There's no org knowledge yet.
2. **Distill.** Click **Distill sessions** in the *Org knowledge* panel. While it runs (10–55s) the button counts
   seconds and the report shows *Summarizing sessions → clustering → writing playbooks*. The report then shows
   15 sessions, 4 recurring clusters and per-phase timings:
   - **created**: vault-token playbook (Priya, Marcus, Sofia resolved it; Tom didn't)
   - **skipped**: Docker builds, *resolved by only one employee*
   - **skipped**: Kafka lag, *recurring, but nobody has resolved it yet*
   - **created**: home-office stipend playbook (3 employees)
3. **Ask again.** Click the same preset. The right side now gives the real Acme fix: VPN → `acme auth refresh --vault`
   → `staging-deployers` via #platform-access → `ACME_VAULT_ROLE` for CI. It cites the playbook.
4. **Provenance.** Click the citation chip. The playbook shows its trigger and steps, and which employees it was
   distilled from. Tom's unresolved session is flagged *not used for steps*.
5. **No false matches.** Click the third preset (*"image not found in registry"*). It uses the same command but has
   a different error. The playbook is retrieved but **not** applied, so it cites nothing.
6. **New hire (User workspace, `#/user`).** You're viewing as *Dana Kim (new hire)*. Turn on **Playbooks**, start a chat and ask
   about the vault error, then follow up with *"I ran acme auth refresh --vault on the VPN, but acme deploy staging
   still says vault permission denied. Which group do I need and how do I request access?"* The agent continues from
   the playbook's `staging-deployers` step.
7. **HR, without leaking.** Switch to *Noah Fischer* + *HR Agent* and ask about a rejected home-office stipend. The
   answer gives the AcmePeople path, the *Remote Work Stipend* category and the $500 limit. It never mentions another
   employee's cost center.
8. **Under the hood (Trace button / Admin › Traces).** Each request shows its Gemini calls with model, latency,
   tokens and the playbooks used; *Cloud Trace ↗* opens the full span tree.
9. **What's next (Admin › Roadmap).** Working now vs the December round.
