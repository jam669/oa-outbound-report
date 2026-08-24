# OA Outbound Weekly Report — Vendor Managers

Live report of how the Vendor-Manager outbound campaigns are performing.
Refreshes itself every Saturday 06:00 Manila from HubSpot; no manual number-typing.

**Report:** `outbound-report.html` → https://jam669.github.io/oa-outbound-report/outbound-report.html
**Companion:** [BD weekly report](https://jam669.github.io/oa-bd-report/bd-weekly-report.html)

---

## What's in scope

**Every email campaign** — all of it is the vendor-manager play. The waves that
target ops/finance/RCM personas still lead with the vendor ask, and the
enterprise/Deep-Ten campaign is vendor outreach too. 31 campaigns, ~1,940 people
on saved lists. Narrow it by setting `INCLUDE_CAMPAIGNS` in `build_registry.py`.

## Two volume numbers, on purpose

| Metric | Means | Source |
|---|---|---|
| **Outreach sent** | campaign **emails** sent, follow-ups included, **no LinkedIn** | weekly update (EOW) |
| **People reached** | distinct **humans** on campaign lists saved on disk | HubSpot email logging |

They do not match and shouldn't — different units over different windows.
5,546 emails reached 1,894 people because most people got two to four
follow-ups, and several waves were built in-session without their list ever
being saved, so those recipients count as outreach but cannot be matched to a
person. For the week of 10–15 Aug the EOW reported 563 sends; the registry could
see only 171, because property management, trades/HVAC, trucking, law and
insurance had no sendlist on disk.

Outreach is **campaign email only**: candidate sourcing is recruitment work and
is excluded (`channels_excluded` in weekly.json), and LinkedIn is tracked
separately in `linkedin` rather than in the email total.

## Identifying Jam's outreach

`hs_created_by_user_id = 66317048` is the only field that reliably separates his
sends from the rest of the team's — `hubspot_owner_id` is round-robin and does not
say who sent the email. Verified against contacts proven his in Gmail. That gives
a fully automatic weekly count (`contacted` / `new_people` in the trend) with no
Gmail access and no EOW dependency. It counts PEOPLE, not sends, so a heavy
follow-up week reads low against the EOW figure; both are shown.

A week with no EOW yet is marked **provisional** on the page and its outreach
comes from this HubSpot count. The EOW supersedes it when sent.

## Replies: why HubSpot is not used

`hs_sales_email_last_replied` is **not** reported as a reply, because it does not
measure replies. Mailsuite emails a *"your email has not been opened yet"* reminder
into the thread 24h after each send, and HubSpot logs that as a reply on the
contact. Across 1,894 contacts, of 881 "replies" **426 landed at almost exactly
+24h** and only 2 fell in the 1–23h window where genuine replies would sit — the
field correlates with prospects *not* reading the email.

Genuine replies come from `connected_email` in weekly.json (the EOW's own
"Connected To"), which is human-verified and excludes auto-replies, out-of-office
and bounces. 25 across five weeks — a 0.45% reply rate, against the 46.5% the
HubSpot field implied. The raw figure is still carried as `thread_notifications`
so it stays traceable, but is never presented as engagement.

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
still sent — no drafts remain unsent — but have no matching CRM record
(suppressed before sending, hard-bounced, or a logging gap), so they cannot be
counted as people reached.

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

**Lead Source is the deciding vote.** A deal that came out of this campaign is
tagged `Lead Source = Vendor Campaign` on the deal in HubSpot. Only tagged deals
are counted; anything else is listed under "not counted" for review. That is what
keeps unrelated work out of the report — Thriviae and Deltabit are real deals but
they did not come from this campaign, so they do not appear here at all.

Association still decides *which* campaign gets the credit, two ways, both
verified against the first two deals:

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
export GMAIL_USER=jam@outsourceaccelerator.com
export GMAIL_APP_PASSWORD=...   # Google app password, not the account password
python update_outbound_report.py
```

The Gmail pair is optional. Without it the report still runs and every outcome
metric is unaffected, but volume falls back to "people reached" and under-counts —
the page says so where the number is read.

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

`.github/workflows/weekly-update.yml` runs Saturdays 06:00 Manila (with a 09:00
backup) and on manual dispatch. Note the cron says Friday 22:00 UTC — Manila is
UTC+8, so that is Saturday morning locally. Repository secrets:

- `HUBSPOT_TOKEN` — same token the BD report uses. **Required.**
- `OUTREACH_CSV_URL` — the published CSV from `OutreachCounts.gs`. Without it,
  outreach volume falls back to HubSpot and under-counts.
- `GMAIL_USER` / `GMAIL_APP_PASSWORD` — an IMAP fallback that **does not work on
  this account**: the Workspace admin has app passwords disabled. Kept for the
  case where that policy changes.

### Counting sends when app passwords are disabled

`OutreachCounts.gs` runs inside the OA Outreach Tracker workbook as Jam, counts
sent mail with `GmailApp` (no admin approval, no app password), and writes a
`week_start,sends,people` tab that gets published as CSV. Setup steps are in the
header of that file. It refreshes Saturday 04:00 Manila, two hours before the
report job reads it.

The workflow does **not** rebuild `campaigns.json`; that file is committed,
because the campaign sources live in the VS Code workspace, not in this repo.
