# 10. What's next — v1.1

The ordering rule is **doors before dashboards**: a thing that lets a system
talk to BRAINS outranks a thing that shows a human what BRAINS did. Every item
in Horizon 1 is a door. Everything that renders is below the line.

## Horizon 1

The platform steps. These ship in order.

**1. Ingestion, including batch.** Leads enter today only as a side effect of
asking for a decision — `ingestion.upsert_lead` has exactly one non-test caller,
`POST /decisions/trigger`. Batch triggering is the gap: one call carrying N
leads.

The rate-limit decision, made explicitly: **a batch of N charges N against the
per-key hourly limit.** There is no separate batch ceiling. The limit exists to
bound what one credential can spend, and a batch endpoint with its own budget
would be a way around it rather than a use of it. Batch size is capped at
**20** — one full hour's budget in a single call — and a batch that would exceed
the key's remaining budget is refused **whole**, with a 429 naming what was
requested and what remained. Rejecting atomically is the point: a caller who
sends 15 and gets a partial success has to work out which leads ran, and that
is a worse position than being told plainly to send fewer.

**2. Per-org gate configuration.** `gate_config_for(org_id)` reading
`org_settings` rows, and `propose(decision, config=None)` defaulting to today's
constants so the existing gate tests pass untouched. The gate stops being one
company's policy compiled into the binary and becomes a per-tenant one, without
the default path changing behaviour at all.

**3. Demo video.** The trace is the argument, and the argument currently
requires reading a README. A recorded run of gather → decide → gate → trail is
the shortest path from "interesting" to "understood".

## Demo assets

Portfolio showpieces, not platform steps — they render what the platform already
knows rather than extending what it can do. Built when an interview or a partner
demo makes them worth building, and not before.

**4. Knowledge tab.** Browse the RAG corpus and the permission filter's effect
on it, per role.

**5. Analytics tab.** Decision volume, auto-execute vs. review rates, gate
blocker frequency. Distinct from item 13: this renders what the seeded database
already contains, where item 13 measures agreement against real partner usage.

## Horizon 2 — From demo to platform (months 2–3)

**6. `decision_type` generalization.** A column on `decisions`, a rules registry
keyed by type, per-type gate config in `org_settings`. Lead qualification becomes
the FIRST type, not the only one. *Unlocks:* structurally, the moment
"lead-scoring demo" becomes "decision platform" — the schema foundation decision
packs sit on. *Split:* schema and API drafted, rules registry design is mine.

**7. Outcome capture.** An `outcomes` table (contacted? converted? deal value?)
fed by an inbound endpoint, linked to decisions. *Unlocks:* ground truth. Cheap
now, and the entire ML story is blocked without it — design partners in shadow
mode generate labeled training data as a side effect.

**8. Similar-deals evidence + duplicate detection.** Embed closed-deal profiles
into a `deal_embeddings` table; new MCP tool `find_similar_deals(lead_id)`
returning the 3 nearest past deals with outcomes as evidence; near-identical lead
embeddings flag as a probable-duplicate blocker. *Unlocks:* pgvector's second
job; "leads like ones we've won" as evidence. *Split:* embedding pipeline
drafted, the tool and evidence wiring is mine (it touches the loop).

**9. First real integration: HubSpot.** Their workflow webhooks call the
ingestion API; a small sync maps HubSpot properties to the lead schema.
**Requires bare-ingest endpoint; decide its rate limit then.** *Unlocks:* the
seeded database stops being the only data source — a design partner connects in
an afternoon.

**10. Approvals where people live: Slack.** Slack app with Approve/Reject
buttons driven by the webhooks, calling the existing approve/reject endpoints.
*Unlocks:* the "no new tool for end users" principle, demonstrated.

**11. Console maturation.** Key management UI (mint/revoke via
`auth.create_key`), per-decision-type policy editing, decision detail
deep-links. Promote `policy_source` to an indexed column when the console
filters on it. *Unlocks:* onboarding an org without touching `psql`.

## Horizon 3 — Earn the platform claims (months 4–6)

**12. First decision pack.** Formalize what a design partner configured — tool
scopes, rules version, routing, autonomy defaults, knowledge templates — as
importable config, not code. *Unlocks:* build-horizontal-sell-vertical gets its
unit of sale.

**13. Shadow-mode analytics.** Agreement rate between gate rulings and human
decisions over time, per rule. *Unlocks:* "Brains agreed with your team on X% of
decisions last month" — the sales chart, from real usage.

**14. Threshold calibration (the first ML, data permitting).** Where humans
ALWAYS approve what the gate held, recommend threshold changes with evidence.
Interpretable models only; ML remains an evidence input, never the gate. Mine,
with review.

**15. Hardening for enterprise.** Split `/internal/*` onto a second Cloud Run
service with `--no-allow-unauthenticated` (the tradeoff the README already
names), `db-g1-small` when a partner goes live, and the documented
run-it-in-your-VPC deploy. *Unlocks:* the CISO conversation closes.

**16. ICP 2 mode.** The same engine at 100% human-in-the-loop with a single
integration, surfaced as "today's recommended moves." *Unlocks:* the second
market, from config — only after a pack exists to template from.

## The GTM thread running through all of it

The build alone anchors nothing. Alongside Horizon 1: the five discovery
conversations and vertical choice (this decides pack #1 and the demo scenario).
During Horizon 2: land 1–3 design partners in shadow mode (HubSpot + Slack make
that a one-afternoon setup). During Horizon 3: one partner into gated or
threshold mode with measured time savings. Every Horizon 3 item consumes what
partners generate — outcomes, agreement stats, pack definitions — which is why
the sequence can't be reordered: data capture before ML, partners before packs,
doors before dashboards.

## Already resolved — not planned work

**Webhook delivery is asynchronous, and has been since `a467913`** ("phase 6:
the integration surface"). The plan's original item 3 was to make it async; that
was written against the pre-phase-6 tree and is wrong, so it is a note here
rather than work in Horizon 1. Verified in the current tree:

- `webhooks.emit` records the delivery row, then calls
  `tasks.enqueue_delivery` — one task per endpoint.
- `tasks.enqueue_delivery` uses the same `_create_cloud_task` helper as
  `/internal/process`: same queue, same OIDC signer, same audience, differing
  only in `path="/internal/deliver"`. The local fallback (`_deliver_in_thread`)
  is gated on `emulated()`.
- Outside tests, `webhooks.deliver` has exactly two callers: the
  `/internal/deliver` handler and that local thread. **Nothing calls it inside
  `decisions.process`**, so no receiver can hold the processing handler open
  toward its 300s timeout and provoke a spurious retry of a decision that
  already completed.

Recorded here so it does not resurface as planned work a third time.
