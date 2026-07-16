---
permitted_roles: [sales, ops, admin]
---

# Territory & Routing Rules

How qualified leads are routed to account executives once approved.

## Regional ownership

- **North America (US, CA):** routed to the AMER pod.
- **UK & Ireland:** routed to the EMEA-West pod.
- **EU (DE, FR, NL, and rest of EU):** routed to the EMEA-Central pod.
- **Rest of world:** held in a global queue for manual assignment.

## Segment overrides

- **Enterprise ($100M+ revenue or 500+ employees):** always routed to a named
  enterprise AE, regardless of region.
- **Existing customers** (any company with a won deal on file) are routed to the
  incumbent account owner, not to a new-business AE, to avoid channel conflict.

## Handling notes

- A company with an **open support ticket** should be flagged to the account
  owner before any new sales outreach — do not cold-route over an active
  support issue.
- Leads matching an existing company in the CRM inherit that company's owner.
