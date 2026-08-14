# OA Outbound Weekly Report — Vendor Managers

Live report of how the Vendor-Manager outbound campaigns are performing.
Refreshes itself every Tuesday 09:00 Manila from HubSpot; no manual number-typing.

**Report:** `outbound-report.html` → https://jam669.github.io/oa-outbound-report/outbound-report.html
**Companion:** [BD weekly report](https://jam669.github.io/oa-bd-report/bd-weekly-report.html)

---

## What's in scope

Three campaigns, 268 targeted people:

| Campaign | Targeted |
|---|---|
| Vendor-Manager ICP (incl. Function-Head backfill) | 168 |
| Apollo Wave 01 — Vendor Managers | 50 |
| Apollo Wave 02 — Vendor Managers | 50 |

Other Apollo waves carry a vendor-style ask but target ops/finance personas —
a different motion, deliberately excluded. To widen scope, add campaign ids to
`INCLUDE_CAMPAIGNS` in `build_registry.py`, re-run it, and commit `campaigns.json`.

---

## How the numbers are produced

```
01_Campaigns/**  ──build_registry.py──▶  campaigns.json   (who we targeted, per campaign)
                                              │
                          HubSpot API ────────┤
                                              ▼
                                update_outbound_report.py
                                              │
                          data.json  +  outbound-report.html  ──▶  GitHub Pages
```

**Sends are verified, not assumed.** HubSpot creates a contact whenever an email
is logged (`Record source` = `EXTENSION` or `BCC_TO_CRM`). A registry address
appearing in HubSpot that way — or carrying a "last contacted" stamp, which covers
people already in the CRM — counts as sent. Addresses that never appear were
drafted but never sent, and show as **Drafted, not sent**.

**Replies are an upper bound.** `hs_sales_email_last_replied` is the only reply
signal available on the contact record (email engagement objects are permission-
blocked on this portal), and it counts any inbound message on the thread —
out-of-office and bounce notifications included. The page says so; scan the reply
list before treating a row as a lead.

**Deals are attributed two ways.** Directly (deal on the contact we emailed) and
via their company — the latter catches referrals, where a vendor manager hands us
to a colleague and the deal lands on the colleague's record. Each deal is counted
once, for the first campaign that reached it.

---

## The attribution gap

At first build, **no call from these campaigns existed as a deal in any HubSpot
pipeline**, and `hs_lead_status` was unset on every vendor-manager contact. The
conversion figures were therefore zero — measuring what had been *logged*, not
what happened. Two fixes, both supported:

1. **Log outbound-sourced calls as deals** against the contact or their company.
   They then appear here automatically, forever, with no manual step. This is the
   real fix.
2. **`wins.csv`** — a stopgap for calls that already happened but were never
   logged. Columns: `date,company,contact,campaign,outcome,note`. These rows are
   marked **Manual** on the page and never blended into CRM figures. Delete a row
   once the deal exists in HubSpot.

---

## Running it

```bash
pip install requests
export HUBSPOT_TOKEN=...        # Private App token: contacts + deals read scopes
python update_outbound_report.py
```

Rebuild the campaign registry after adding a wave (needs the VS Code workspace,
so this is a local-only step):

```bash
python build_registry.py        # rewrites campaigns.json — commit the result
```

Check the page still renders after editing the HTML:

```bash
python _smoke_test.py           # renders 3 scenarios against a stubbed DOM
```

## Automation

`.github/workflows/weekly-update.yml` runs Tuesdays 09:00 Manila (with a 12:00
backup) and on manual dispatch. It needs one repository secret:

- `HUBSPOT_TOKEN` — same token the BD report uses.

The workflow does **not** rebuild `campaigns.json`; that file is committed,
because the campaign sources live in the VS Code workspace, not in this repo.
