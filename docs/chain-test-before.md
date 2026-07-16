# Chain test — qualifying priya@acmerobotics.com (before)

Date: 2026-07-16
DB state: `db/seed.sql` as originally seeded (org 1 only), container `brains-db` up and healthy.

## Verdict

**Route her — auto-route.** Score **100 / 100**, band **hot**. She clears the
auto-route bar with room to spare, and the CRM shows an existing open deal with
her company, so she should land with the account/deal owner rather than a cold
SDR queue.

## Exactly what I did

Three tool calls, in this order. `lookup_lead` first because it is the only
tool that resolves an email; the other two both consume its output and are
independent of each other, so I issued them together.

### 1. `lookup_lead` — resolve the email to a lead + company

- **Argument:** `email = "priya@acmerobotics.com"` (verbatim from your request).
- **Returned:**
  ```json
  {
    "found": true,
    "lead": {
      "id": 1,
      "email": "priya@acmerobotics.com",
      "first_name": "Priya", "last_name": "Sharma",
      "title": "VP of Operations",
      "source": "webinar",
      "created_at": "2026-07-15T19:21:04.752944+00:00"
    },
    "company": {
      "id": 1, "name": "Acme Robotics", "industry": "Manufacturing",
      "employee_count": 1200, "annual_revenue_usd": 250000000, "country": "US"
    }
  }
  ```
- **What I extracted for the next calls:** `lead.id = 1` and `company.name = "Acme Robotics"`.

### 2. `check_crm` — pull the company's CRM record

- **Argument:** `company = "Acme Robotics"`.
- **Returned:**
  ```json
  {
    "found": true,
    "company": { "id": 1, "name": "Acme Robotics", ... },
    "deals":   [ { "id": 1, "name": "Acme pilot expansion", "stage": "open", "amount_usd": 45000, "closed_at": null } ],
    "tickets": [ { "id": 3, "subject": "Onboarding docs request", "status": "resolved", ... } ],
    "open_deals": 1,
    "open_tickets": 0
  }
  ```
- **Signal:** 1 open deal, 0 open tickets — an active, healthy account with a deal already in flight.

### 3. `score_lead` — score the lead

- **Argument:** `lead_id = 1`.
- **Returned:**
  ```json
  {
    "found": true, "lead_id": 1, "email": "priya@acmerobotics.com",
    "title": "VP of Operations", "company": "Acme Robotics",
    "score": 100, "band": "hot",
    "reasons": [
      "senior title: 'VP of Operations' matches 'vp' (+30)",
      "employee_count 1200 >= 500 (+25)",
      "annual_revenue_usd 250000000 >= 100M (+25)",
      "source 'webinar' is high intent (+20)"
    ],
    "rules_version": "1.0.0"
  }
  ```

## How I decided what to pass

- **`check_crm(company=...)`** — the tool takes a company *name* string (its only
  parameter). The natural source is `company.name` from the `lookup_lead`
  result, so I passed `"Acme Robotics"`. I did *not* pass `company.id` (1)
  because the tool has no id parameter.
- **`score_lead(lead_id=...)`** — the tool takes an integer `lead_id`. The only
  place I have that is `lead.id` from `lookup_lead`, so I passed `1`.

## Ambiguities / things I had to guess

1. **The routing threshold is not defined by any tool.** `score_lead` returns a
   numeric score and a band (`hot`/`warm`/`cold`), but no docstring says "route
   if hot" or "route if score ≥ N". I inferred the auto-route bar from the
   scoring bands (`band_for`: ≥80 = hot) and the seed-file annotations
   (a "score 100 → AUTO-ROUTE" row and a "score 65 … under the 80 gate → human"
   row). So the *route/hold* decision rests on an inferred 80 gate, not on
   anything the tools state. At 100 the answer is unambiguous either way, but
   the threshold itself was a guess.

2. **`check_crm` keys on a bare company name with no org scoping.** The docstring
   ("Look up a company's CRM record") does not say the name must be unique or
   which org it resolves within. Passing `"Acme Robotics"` is fine today because
   there is exactly one such company, but the schema's uniqueness is
   `(org_id, name)` — the name alone is *not* globally unique. In a multi-org
   database a bare-name lookup is ambiguous. It worked here only because the
   data is single-org; that's a latent guess about which company I'm getting.
   (This is exactly what part 2's composite-unique test exercises.)

3. **What "qualify" needs from `check_crm` is unstated.** `score_lead` alone
   produces the number. I called `check_crm` because "qualify … and route"
   implies checking the account context (open deals/tickets), and the CRM signal
   changes *where* she routes (existing open deal → account owner) even though it
   doesn't change the score. No docstring says the CRM check feeds the routing
   decision — I treated it as routing context, not as a scoring input.

4. **Call order was inferred from data dependencies, not documented.** Nothing in
   the docstrings prescribes an order. `lookup_lead` must come first (only
   email→id/name resolver); `check_crm` and `score_lead` each depend on its
   output but not on each other, so their relative order is arbitrary — I ran
   them concurrently.
