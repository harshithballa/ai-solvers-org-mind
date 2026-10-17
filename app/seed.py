"""Synthetic Acme Corp data. Messages are scripted; summaries/clusters/playbooks are computed live."""
from datetime import timedelta

from app.models import Agent, Employee, Message, Org, Session, now
from app.prompts import DEV_AGENT_PROMPT, HR_AGENT_PROMPT
from app.store import Store

ORG_ID = "acme"

EMPLOYEES = [
    ("e_priya", "Priya Raman", "Backend Engineer", "engineering", "#e07a5f"),
    ("e_marcus", "Marcus Lee", "Platform Engineer", "engineering", "#3d405b"),
    ("e_sofia", "Sofia Alvarez", "Frontend Engineer", "engineering", "#81b29a"),
    ("e_tom", "Tom Okafor", "Data Engineer", "engineering", "#f2cc8f"),
    ("e_dana", "Dana Kim", "Software Engineer (new hire)", "engineering", "#6d597a"),
    ("e_liam", "Liam Chen", "Account Executive", "sales", "#457b9d"),
    ("e_aisha", "Aisha Patel", "Product Designer", "design", "#e56b6f"),
    ("e_noah", "Noah Fischer", "Finance Analyst (new hire)", "finance", "#2a9d8f"),
]

AGENTS = [
    Agent(id="a_dev", org_id=ORG_ID, kind="dev", name="Dev Agent", team="engineering",
          description="Internal tooling, CI/CD, deploys, local dev setup.", system_prompt=DEV_AGENT_PROMPT),
    Agent(id="a_hr", org_id=ORG_ID, kind="hr", name="HR Agent", team="people",
          description="Benefits, leave, expenses, payroll, HR portal.", system_prompt=HR_AGENT_PROMPT),
]

# (employee_id, agent_id, days_ago, [user, assistant, user, assistant, ...])
SESSIONS: list[tuple[str, str, int, list[str]]] = [
    # ---- Dev: recurring staging deploy / vault token issue (3 resolved, 1 unresolved) ----
    ("e_priya", "a_dev", 12, [
        "`acme deploy staging` fails with `Error: vault: permission denied (ACME_VAULT_TOKEN expired)`. It worked last week. What's going on?",
        "That error means the Vault token used by the deploy tool has expired. Usually you can re-authenticate with `vault login` and export a fresh token as `ACME_VAULT_TOKEN`, then retry the deploy.",
        "`vault login` isn't installed on our laptops. Is there an Acme way to do this?",
        "I don't know the Acme-specific command for this. Check the deploy tool's help output (`acme --help`) for an auth subcommand, or ask the platform team.",
        "Found it: `acme auth refresh --vault` regenerates the token. Platform said tokens now expire every 7 days since the platform migration. Ran it, deploy worked.",
        "Great, glad that worked. Running `acme auth refresh --vault` whenever the token expires should keep deploys unblocked.",
    ]),
    ("e_marcus", "a_dev", 9, [
        "Our staging pipeline in acme-ci is failing at the deploy step: `vault: permission denied (ACME_VAULT_TOKEN expired)`. I already ran `acme auth refresh --vault` locally and it printed nothing.",
        "If the refresh printed nothing, it may not have succeeded. Check whether the command returns a non-zero exit code and whether you have network access to the Vault server.",
        "You were right, it silently fails off VPN. Connected to `acme-corp-vpn`, ran `acme auth refresh --vault` again and got 'token refreshed'. CI still failed though.",
        "CI jobs often use a separate identity from your laptop. Check which Vault role the pipeline uses and whether it has a valid token.",
        "Yep. Re-ran the pipeline with the variable `ACME_VAULT_ROLE=staging-deployer` set and it went green. That fixed it.",
        "Nice. So: refresh on VPN, and make sure the CI job runs with `ACME_VAULT_ROLE=staging-deployer`.",
    ]),
    ("e_sofia", "a_dev", 6, [
        "Getting `vault: permission denied (ACME_VAULT_TOKEN expired)` on `acme deploy staging`. I ran `acme auth refresh --vault` on VPN like Priya told me but it still says permission denied.",
        "If the token is fresh but you still get permission denied, your identity may lack access to the staging secrets path. Check your group memberships or ask whoever administers Vault.",
        "Turns out I wasn't in the `staging-deployers` group. Requested access in the #platform-access Slack channel, got approved in about an hour, refreshed again and the deploy worked.",
        "Good to know, thanks for sharing. Membership in `staging-deployers` is needed in addition to a fresh token.",
    ]),
    ("e_tom", "a_dev", 3, [
        "`acme deploy staging` keeps failing with `ACME_VAULT_TOKEN expired`. Any idea?",
        "The token used by the deploy tool has expired. Try re-authenticating and make sure your credentials are current.",
        "I rotated my SSH key and reinstalled the acme CLI. Still failing with the same error. I'll give up for today.",
        "Sorry it's still failing. You may want to contact the platform team.",
    ]),
    # ---- Dev: same employee repeating a problem (must NOT become a playbook) ----
    ("e_marcus", "a_dev", 15, [
        "Docker builds on my Linux laptop are really slow, every `docker build` reinstalls all pip deps.",
        "Make sure dependency installation happens in a layer before copying your source code, and enable BuildKit cache mounts: `RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt`.",
        "Moved the COPY after pip install and added the cache mount. Builds went from 6 min to 40s, that fixed it.",
        "Great improvement!",
    ]),
    ("e_marcus", "a_dev", 4, [
        "Docker builds slow again on my laptop, pip deps reinstall every docker build.",
        "Check that BuildKit is enabled (`DOCKER_BUILDKIT=1`) so the cache mount is honored.",
        "BuildKit was off after a Docker update. Set DOCKER_BUILDKIT=1, works now.",
        "Glad it's sorted.",
    ]),
    # ---- Dev: unrelated noise ----
    ("e_priya", "a_dev", 8, [
        "How do I write a pytest fixture that gives each async test its own Postgres transaction?",
        "Use an async fixture with `pytest-asyncio`: open a connection, begin a transaction, yield the session, then roll back in teardown so each test starts clean.",
        "Rollback-per-test fixture works now, thanks.",
        "You're welcome!",
    ]),
    ("e_sofia", "a_dev", 10, [
        "Cypress tests are flaky in CI with `Timed out retrying after 4000ms` on the checkout page.",
        "Avoid fixed waits; wait on the network request with `cy.intercept` + `cy.wait('@alias')`, and consider raising `defaultCommandTimeout` for slow CI runners.",
        "Switched to cy.intercept and waiting on the alias, flakiness is fixed.",
        "Great!",
    ]),
    # ---- HR: recurring home-office stipend rejection (3 distinct employees, all resolved) ----
    ("e_sofia", "a_hr", 20, [
        "My home-office stipend claim in AcmePeople got rejected with 'Missing cost center'. What does that mean?",
        "It sounds like the claim couldn't be routed to a budget. I don't know the exact AcmePeople setting, but usually your cost center needs to be on your employee profile. Your HR business partner can confirm.",
        "Found it: AcmePeople > Profile > Job Info > Cost Center was blank. Filled it in (ENG-200), resubmitted the claim and it went through.",
        "Glad that worked!",
    ]),
    ("e_liam", "a_hr", 14, [
        "My expense for a monitor for working from home was rejected in AcmePeople, 'Missing cost center'. I'm in sales.",
        "That error usually means your profile has no cost center. Check your profile in AcmePeople and ask your manager for the right cost center code.",
        "Set my cost center under Profile > Job Info, but it was rejected again. Finance told me home-office items must be filed under the category 'Remote Work Stipend', not 'Office Equipment'. Refiled under that category and it was approved.",
        "Thanks for the update, good to know about the category.",
    ]),
    ("e_aisha", "a_hr", 7, [
        "AcmePeople rejected my home-office chair claim ($640). It says 'Missing cost center' and something about the limit.",
        "Make sure your cost center is set on your profile. For the limit, I don't know Acme's exact stipend amount; check the expense policy.",
        "Policy says the Remote Work Stipend is $500 per year and receipts must be uploaded within 60 days of purchase. Set my cost center, filed $500 under 'Remote Work Stipend' with the receipt, approved.",
        "Great, glad it's approved.",
    ]),
    # ---- HR: unrelated noise ----
    ("e_marcus", "a_hr", 11, [
        "How many PTO days can I carry over into next year?",
        "I don't know Acme's exact carryover policy; it's usually in the employee handbook or you can ask HR.",
        "Found it in the handbook: up to 5 days carry over and must be used by March 31. Thanks, resolved.",
        "Thanks for sharing!",
    ]),
    # ---- Dev: recurring but nobody resolved it (must NOT become a playbook) ----
    ("e_priya", "a_dev", 5, [
        "The `orders-consumer` service on staging shows Kafka consumer lag climbing past 50k on the `acme-events` topic. Restarting doesn't help.",
        "Consumer lag usually means consumers can't keep up. Check partition count vs consumer instances and look for slow message handlers.",
        "Partitions and instances match, handlers look fine. Still lagging, I'll escalate later.",
        "Sorry I couldn't pin it down. Escalating to the platform team is a good next step.",
    ]),
    ("e_tom", "a_dev", 2, [
        "Kafka consumer lag on `acme-events` in staging keeps growing for `orders-consumer`, now over 80k.",
        "Try scaling the consumer group or checking for rebalancing storms in the logs.",
        "Scaled to 6 replicas, lag still grows. Didn't fix it, no idea what's wrong.",
        "Sorry, I don't have more Acme-specific guidance for this.",
    ]),
    # ---- Dev: ONE employee works out a reusable procedure (skill extraction; must NOT become a playbook) ----
    # Contains one personal value (Dana's sandbox project) that must never appear in an extracted skill.
    ("e_dana", "a_dev", 1, [
        "I need to query the Acme analytics dataset in BigQuery (table `acme-analytics.events.orders`) from my laptop. How do I authenticate?",
        "Use Application Default Credentials: run `gcloud auth application-default login` and sign in with your Acme Google account. Service-account key files aren't allowed on laptops.",
        "Logged in. Now `bq query \"SELECT COUNT(*) FROM acme-analytics.events.orders\"` fails with `Access Denied: Project acme-analytics: User does not have bigquery.jobs.create permission`.",
        "You can read `acme-analytics`, but query jobs must run in a project you're allowed to bill. Pass your own sandbox project with `--project_id=<your sandbox project>`.",
        "My sandbox is acme-sbx-dkim-4821. With `--project_id=acme-sbx-dkim-4821` it now says `Syntax error: Unexpected \"-\"` near the table name.",
        "That's the gotcha: `bq query` defaults to legacy SQL. Add `--use_legacy_sql=false` and wrap the table name in backticks for standard SQL, e.g. FROM `acme-analytics.events.orders`.",
        "That worked: bq query --project_id=acme-sbx-dkim-4821 --use_legacy_sql=false 'SELECT COUNT(*) FROM `acme-analytics.events.orders`' returned the row count. Thanks!",
        "Great. To recap: ADC login once, then `bq query` with `--project_id` set to your own sandbox project and `--use_legacy_sql=false`, with the table in backticks.",
    ]),
]

BIGQUERY_SESSION_ID = "ses_seed_14"  # index in SESSIONS above
BIGQUERY_PERSONAL_VALUE = "acme-sbx-dkim-4821"


def seed(store: Store) -> None:
    store.reset()
    store.put(Org(id=ORG_ID, name="Acme Corp"))
    for eid, name, role, team, color in EMPLOYEES:
        store.put(Employee(id=eid, org_id=ORG_ID, name=name, role=role, team=team, avatar_color=color))
    for a in AGENTS:
        store.put(a)

    agents = {a.id: a for a in AGENTS}
    t0 = now()
    for i, (eid, aid, days_ago, turns) in enumerate(SESSIONS):
        start = t0 - timedelta(days=days_ago)
        title = turns[0].replace("`", "")[:70]
        session = Session(id=f"ses_seed_{i:02d}", org_id=ORG_ID, employee_id=eid, agent_id=aid,
                          agent_kind=agents[aid].kind, title=title, status="closed",
                          created_at=start, updated_at=start + timedelta(minutes=5 * len(turns)))
        for j, text in enumerate(turns):
            store.add_message(Message(session_id=session.id, role="user" if j % 2 == 0 else "assistant",
                                      content=text, created_at=start + timedelta(minutes=5 * j)))
        store.put(session)
