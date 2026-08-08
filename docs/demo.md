# Demo runbook

Read-and-do. Five beats, in order, roughly six minutes. Every command here is
literal — paste it, watch the thing happen, move on.

The scores and blockers below were computed from the seed data rather than
copied from the comments in `db/seed.sql` — one of which was out of date when
this was written, and has since been corrected to match. If you change
`rules.py` or the seed, re-derive them before recording.

## Before you start

```bash
export URL="$(./scripts/deploy.sh url)"
export KEY="brains_sk_..."        # a sales key; see ./scripts/deploy.sh keys
```

Open the viewer in a second window and connect it with the same key:

```
$URL/console
```

Leave it on the list view. Every beat below ends by pointing at it.

**Check the budget before recording.** Each trigger charges one of 20 per key
per hour, and a full run-through spends four. A second take in the same hour is
fine; a fifth is not. `curl -s "$URL/decisions?limit=1" -H "X-API-Key: $KEY"`
costs nothing — only triggers are charged.

## The five beats

### 1. Spam is discarded without asking anyone

**Lead:** `sam@karmatextiles.in` — Sam Okoro, Data Analyst at Karma Textiles,
cold list. Score **0**, no blockers.

```bash
curl -s -X POST "$URL/decisions/trigger" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"email": "sam@karmatextiles.in"}'
# 202 {"decision_id": N, "status": "processing"}
```

Wait ~15s, then:

```bash
curl -s "$URL/decisions/$N" -H "X-API-Key: $KEY" | jq '.status, .reasoning.gate.rule'
# "auto_discarded"
# "auto_discard:below_floor"
```

**The point:** nobody was asked. Score 0 is below the floor of 20, the model
agreed the action was `discard`, and no blocker fired — so the gate discarded it
on its own. Autonomy at the bottom end is what makes the queue worth looking at.

> **Do not use `noreply@leadgenblast.co` for this beat**, even though it is the
> obvious "spam" address in the seed. It has no company, so `blocker:no_company`
> fires and it lands in `pending_approval` instead — blockers outrank the
> discard floor. It is a good beat six if you want one; it is the wrong beat one.

### 2. A strong lead executes without asking anyone

**Lead:** `priya@acmerobotics.com` — VP of Operations at Acme Robotics (600
staff, $80M), from a webinar. Score **100**, no blockers. Acme has an *open*
deal and a *resolved* ticket, neither of which gates.

```bash
curl -s -X POST "$URL/decisions/trigger" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"email": "priya@acmerobotics.com"}'
```

```bash
curl -s "$URL/decisions/$N" -H "X-API-Key: $KEY" | jq '.status, .score, .reasoning.gate.rule'
# "auto_executed"
# 100
# "auto_execute:threshold_met"
```

**The point:** the other end of the same policy. 100 ≥ 80 with nothing blocking,
so it routed itself. Two beats in, nothing has reached a human — which is the
setup for beat three.

### 3. Mark is blocked, and the model explains itself

**Lead:** `mark@nimbushealth.com` — Director of IT at Nimbus Health. Score
**90**, hot, an existing customer with a **won** deal — *and* an open support
ticket. `blocker:open_tickets` fires.

```bash
curl -s -X POST "$URL/decisions/trigger" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"email": "mark@nimbushealth.com"}'
```

Now **switch to the viewer**, refresh, and open the new row. On screen:

- status `pending_approval`, score 90, band `hot`
- **Gate**: rule `blocker:open_tickets`, and a `policy: defaults` badge
- **Evidence**: `has_company: true, lost_deals: 0, open_tickets: 1`
- **Model proposal**: `route_to_sales`, confidence high, with the rationale in
  the model's own words — it argues *for* routing Mark, because on the numbers
  he is the best lead in the set

**The point, and it is the whole demo:** the model wanted to route him. The gate
said no, because a customer with an open ticket is not someone to pitch to this
morning. A rule caught what a good score would have missed — and the trace shows
both the argument and the override, side by side.

Read the rationale aloud from the screen. That paragraph is the demo.

### 4. Approve it from where the work already happens

Mark is sitting in `pending_approval`. The `proposed` webhook has already fired
to whatever is subscribed.

**With Make (or Zapier) wired up:** the scenario receives the webhook, posts to
Slack with Approve / Reject buttons, and the button calls back:

```
POST {{$URL}}/decisions/{{1.decision_id}}/approve
Headers: X-API-Key: {{connection.key}}, Content-Type: application/json
Body:    {"decided_by": "{{slack.user.name}}"}
```

**Without Make wired up**, do the same call by hand — it is the identical
request the button makes:

```bash
curl -s -X POST "$URL/decisions/$N/approve" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"decided_by": "abhishek"}'
# 200 {"id": N, "status": "approved", ...}
```

Run it twice on purpose. The second returns **409**, not a second approval —
the transition is guarded in the UPDATE's WHERE clause, so two people clicking
the same Slack button is a 200 and a 409, never two approvals.

**The point:** no new tool for the humans. The approval happens in Slack; BRAINS
is the thing behind it.

### 5. The whole trace, rendered

Back to the viewer, on Mark's row, now `approved` and stamped `decided_by`.
Scroll the detail pane top to bottom:

- **Gate** — the rule that decided, and `policy_source` saying *whose* policy
  decided it: `defaults` here, `org_settings` for a tenant that configured its
  own, `fallback_unreadable` if the settings could not be read and the gate held
  everything for a human rather than quietly applying someone else's thresholds
- **Evidence** — the three deterministic facts, the only things the gate saw
- **Model proposal** — what the model wanted, kept separate from what happened
- **Trace** — every tool call in order, with arguments and results: `lookup_lead`,
  `check_crm`, `score_lead`, and the scoring `reasons` line by line

Optionally close the loop by recording what actually happened:

```bash
curl -s -X POST "$URL/decisions/$N/outcome" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"outcome": "converted", "value_usd": 60000, "recorded_by": "abhishek"}'
```

Refresh the detail pane — it appears under **Outcomes**. Post a second one with
`{"outcome": "lost"}` and both rows stay, newest first: corrections are new
rows, because ground truth you can edit in place is not ground truth.

**The closing line:** every number on that screen came from a rule you can read,
every action was gated by a threshold you set, and the whole argument is one
row in a table.

## Reset, to run it again

Triggers are additive — leads are upserted, so re-running creates new decisions
rather than failing. You only need this if you want a clean list on camera.

Through the Auth Proxy (the same route `deploy.sh migrate` uses):

```bash
cloud-sql-proxy --port 5433 "$(gcloud sql instances describe brains-pg \
  --format='value(connectionName)')" &

DATABASE_URL="postgresql://brains@127.0.0.1:5433/brains" \
PGPASSWORD="$(gcloud secrets versions access latest --secret=brains-db-password)" \
uv run python - <<'PY'
import os, psycopg
DEMO = ('sam@karmatextiles.in', 'priya@acmerobotics.com',
        'mark@nimbushealth.com', 'noreply@leadgenblast.co')
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        # Outcomes first — they reference decisions.
        cur.execute("""DELETE FROM outcomes WHERE decision_id IN (
                         SELECT id FROM decisions WHERE org_id = 1
                         AND trigger_input->>'email' = ANY(%s))""", (list(DEMO),))
        cur.execute("""DELETE FROM decisions WHERE org_id = 1
                       AND trigger_input->>'email' = ANY(%s)""", (list(DEMO),))
        # Give the hourly budget back, so take two starts from 0/20.
        cur.execute("DELETE FROM api_key_rate_limit")
    conn.commit()
print("demo rows cleared")
PY

kill %1
```

This deletes only decisions whose `trigger_input.email` is one of the four demo
leads, so anything else in the org survives. The leads, companies, deals and
tickets are untouched — they are the fixture, not the output.

For a database with no seed data at all (a fresh instance), run
`./scripts/deploy.sh seed` first. It is safe to re-run: the demo rows bounce off
their unique constraints.
