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

## Attribution — how a call becomes a number here

**The process (from 14 Aug 2026): a Deal is created once a prospect has attended
a DC.** That single habit is what keeps this report honest — every DC becomes a
deal, every deal is attributed back to the campaign that sourced it, and no one
retypes anything.

Both routes are live and verified against the first two deals:

| Deal | Attributed via | Why |
|---|---|---|
| Neighbor — 10 Activation Agents | **direct** | Mark Bailey is on the Vendor-Manager ICP sendlist |
| Humana | **company** | we emailed K. McDaniel; the DC was with Matt Rowley — both hang off the same `Humana` company record |

The company route is what catches referrals, which is the normal shape of a
vendor-manager win: the vendor manager hands you to the person who actually owns
the work, and the deal lands on *their* record.

Before that process existed, no call from these campaigns existed as a deal in any
pipeline, and conversion figures read zero — measuring what had been *logged*, not
what happened. `wins.csv` remains as a stopgap for anything that slips through:
columns `date,company,contact,campaign,outcome,note`, rendered as **Manual** rows
and never blended into CRM figures. Delete a row once the deal exists in HubSpot.
The file is empty by design; both original entries now come straight from the CRM.

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
