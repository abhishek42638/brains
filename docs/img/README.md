# Screenshots referenced by the top-level README

Two files, both PNG, both from the deployed console at
`https://brains-api-xubjv7k5xq-el.a.run.app/console` connected with a **sales**
key. They exist because the console needs an API key to show anything, and a
reader without one would otherwise see an empty shell.

Take them at a browser width of ~1400px so both panes are visible, and after
clicking **Connect** (which clears the key field — check the field is empty in
the frame before capturing).

## `console-list.png`

The list view, populated. Should show:

- the header: `BRAINS`, `read-only decision viewer`, `connected (key held in memory only)`
- the filter row: the status `<select>`, the type `<select>`, `Refresh`
- **at least four rows** covering different outcomes, so the badges differ —
  ideally one `auto_executed`, one `auto_discarded`, one `pending_approval` and
  one `approved`. Each row shows the proposed action, `#id`, and the status /
  score / band / decision-type badges.

Run beats 1–3 of [`../demo.md`](../demo.md) first to guarantee that mix.

## `console-detail-mark.png`

The detail pane for the `mark@nimbushealth.com` decision — the one the README's
trace section is about. Should show, top to bottom:

- `Decision #N`, status `pending_approval`, score 90, band `hot`
- **Gate** — the `blocker:open_tickets` badge, the `policy: defaults` badge, and
  the rule / detail / policy source rows
- **Evidence** — `{"has_company": true, "lost_deals": 0, "open_tickets": 1}`
- **Model proposal** — `route_to_sales`, confidence `high`, and the full
  rationale paragraph. **This is the part that has to be legible**; it is the
  sentence the README quotes ("One open support ticket is unrelated to sales
  qualification…"). Crop or scroll so it is readable rather than fitting the
  whole page in.
- enough of **Trace** below it to show the tool steps continue

If the rationale and the gate badge do not fit in one frame, favour a taller
crop over a smaller font — the point of the shot is that the model's argument
and the override that ignored it are visible together.
