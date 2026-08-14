"""
OA Outbound Weekly Report — updater
===================================
Reads campaigns.json (who we targeted, and under which campaign), asks HubSpot
what happened to those people, and writes the numbers into outbound-report.html.

Run manually:
    python update_outbound_report.py

Environment variable required:
    HUBSPOT_TOKEN   HubSpot Private App token (contacts + deals read scopes)

Requirements:
    pip install requests

WHAT COUNTS AS A SEND
---------------------
HubSpot logs every outbound email and creates the contact if it does not exist,
stamping `Record source` as EXTENSION (Gmail extension) or BCC_TO_CRM. So a
registry address that shows up in HubSpot with one of those sources — or any
address carrying a "last contacted" stamp, which covers people who were already
CRM contacts before we wrote to them — is a verified send. Addresses that never
appear were drafted but never sent. Nothing here trusts a local file's claim
that an email went out.

REPLY CAVEAT
------------
`hs_sales_email_last_replied` is the only reply signal available on the contact
record — email engagement objects are permission-blocked on this portal. That
property counts ANY inbound reply on the thread, including out-of-office
auto-replies and bounce notifications, so reply figures are an upper bound and
are labelled as such on the page.

ATTRIBUTION
-----------
From 14 Aug 2026 the process is: a Deal is created once a prospect has attended
a DC. Deals are then credited back to a campaign two ways:

  direct  — the deal sits on the contact we emailed.
  company — the deal sits on a colleague at the same employer. This is the normal
            shape of a vendor-manager win: the vendor manager hands us to whoever
            owns the work, and the deal lands on their record. Guarded so it only
            counts deals created after our first email to that company, otherwise
            any pre-existing or inbound deal at a large employer would be credited
            to outbound.

A deal is counted once, for the first campaign that reaches it.

wins.csv stays as a stopgap for a call that never made it into the CRM. Rows are
counted separately and marked "Manual" on the page, so they never blur into CRM
figures. Empty by default. See WINS_FILE below.
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ── Config ───────────────────────────────────────────────────────────────────

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
BASE_URL      = "https://api.hubapi.com"
HEADERS       = {"Authorization": "Bearer %s" % HUBSPOT_TOKEN,
                 "Content-Type": "application/json"}

PORTAL_ID      = "44390857"
BD_PIPELINE_ID = "68218158"
MANILA_TZ      = timezone(timedelta(hours=8))

HERE          = os.path.dirname(os.path.abspath(__file__))
REGISTRY_FILE = os.path.join(HERE, "campaigns.json")
DATA_FILE     = os.path.join(HERE, "data.json")
HTML_FILE     = os.path.join(HERE, "outbound-report.html")

# Optional. Calls/meetings that came from these campaigns but were never logged
# as deals in HubSpot. Columns: date,company,contact,campaign,outcome,note
# Delete a row once the deal exists in HubSpot — the CRM figure takes over and
# the page shows it under "logged in CRM" instead.
WINS_FILE = os.path.join(HERE, "wins.csv")

# Weeks shown in the trend series (report weeks run Wed→Tue, matching the BD report).
TREND_WEEKS = 12

# ── BD pipeline stage IDs (shared with the BD weekly report) ─────────────────
STAGES = {
    "deal_created":      "132946329",
    "dc_outreach":       "244709522",
    "dc_completed":      "222405237",
    "dc_no_show":        "132946331",
    "ac_outreach":       "244520495",
    "ac_no_show":        "133003872",
    "ac_completed":      "132946333",
    "cd_main":           "1029860491",
    "cd_scheduled":      "1053002936",
    "cd_no_show":        "1053002935",
    "cd_completed":      "1053002937",
    "hiring_recruiting": "133348729",
    "closed_won":        "132946334",
    "closed_lost":       "132946335",
    "deal_unqualified":  "991351894",
}

STAGE_LABELS = {
    STAGES["deal_created"]:      "Deal Created",
    STAGES["dc_outreach"]:       "DC Outreach",
    STAGES["dc_completed"]:      "DC Completed",
    STAGES["dc_no_show"]:        "DC No Show",
    STAGES["ac_outreach"]:       "AC Outreach",
    STAGES["ac_completed"]:      "AC Completed",
    STAGES["ac_no_show"]:        "AC No Show",
    STAGES["cd_main"]:           "CD Main",
    STAGES["cd_scheduled"]:      "CD Scheduled",
    STAGES["cd_no_show"]:        "CD No Show",
    STAGES["cd_completed"]:      "CD Completed",
    STAGES["hiring_recruiting"]: "Hiring & Recruiting",
    STAGES["closed_won"]:        "Closed Won",
    STAGES["closed_lost"]:       "Closed Lost",
    STAGES["deal_unqualified"]:  "Deal Unqualified",
}

# A deal that reached any of these stages means the Discovery Call was attended.
DC_ATTENDED_STAGES = frozenset({
    STAGES["dc_completed"],
    STAGES["ac_outreach"], STAGES["ac_no_show"], STAGES["ac_completed"],
    STAGES["cd_main"], STAGES["cd_scheduled"], STAGES["cd_no_show"], STAGES["cd_completed"],
    STAGES["hiring_recruiting"], STAGES["closed_won"],
})

# A deal existing at all means a call was booked; these stages mean it is dead.
DEAD_STAGES = frozenset({
    STAGES["closed_lost"], STAGES["deal_unqualified"],
})

CONTACT_PROPERTIES = [
    "email", "firstname", "lastname", "company", "jobtitle",
    "createdate", "hs_object_source_label", "notes_last_contacted",
    "hs_sales_email_last_replied", "hs_lead_status", "lifecyclestage",
    "num_associated_deals", "hubspot_owner_id", "lead_category",
]

DEAL_PROPERTIES = [
    "dealname", "dealstage", "pipeline", "amount", "createdate",
    "closedate", "hubspot_owner_id",
]

# Lead statuses that mean a human on the other end actually engaged.
ENGAGED_STATUSES = {"CONNECTED", "Feedback", "Qualified", "BD - Deal Created", "Converted"}

# Record sources that mean "this contact exists because we emailed them".
LOGGED_SOURCES = {"EXTENSION", "BCC_TO_CRM"}


# ── HubSpot plumbing ─────────────────────────────────────────────────────────

def api_post(path, payload, attempts=5):
    """
    POST with backoff. HubSpot returns transient 400s during heavy paging as
    well as the usual 429/5xx — retrying beats silently truncating the dataset.
    """
    url = BASE_URL + path
    for attempt in range(attempts):
        try:
            resp = requests.post(url, headers=HEADERS, json=payload, timeout=60)
        except requests.RequestException as exc:
            if attempt == attempts - 1:
                raise
            print("   ! network error (%s), retrying" % exc)
            time.sleep(2 ** attempt)
            continue

        if resp.status_code in (200, 207):
            return resp.json()

        if resp.status_code in (400, 429, 500, 502, 503, 504) and attempt < attempts - 1:
            time.sleep(2 ** attempt)
            continue

        raise RuntimeError("HubSpot %s on %s: %s" % (resp.status_code, path, resp.text[:400]))

    raise RuntimeError("HubSpot %s failed after %d attempts" % (path, attempts))


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_contacts_by_email(emails):
    """Look up registry addresses in HubSpot, 100 at a time."""
    found = {}
    batches = list(chunked(emails, 100))

    for idx, batch in enumerate(batches, 1):
        payload = {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "IN", "values": batch}
            ]}],
            "properties": CONTACT_PROPERTIES,
            "limit": 100,
        }
        after = None
        while True:
            if after:
                payload["after"] = after
            data = api_post("/crm/v3/objects/contacts/search", payload)
            for item in data.get("results", []):
                email = (item.get("properties", {}).get("email") or "").lower()
                if email:
                    found[email] = item
            after = (data.get("paging", {}).get("next") or {}).get("after")
            if not after:
                payload.pop("after", None)
                break

        if idx % 5 == 0 or idx == len(batches):
            print("   contacts: batch %d/%d — %d matched so far" % (idx, len(batches), len(found)))

    return found


def fetch_associations(from_type, to_type, from_ids):
    """
    from id → [to id], via the v4 batch association reader.

    Note this is called for EVERY matched contact rather than only those with
    num_associated_deals set — that property comes back empty on this portal
    even for records that do have associations, so gating on it loses deals.
    """
    links = defaultdict(list)
    for batch in chunked([str(i) for i in from_ids], 100):
        payload = {"inputs": [{"id": cid} for cid in batch]}
        data = api_post("/crm/v4/associations/%s/%s/batch/read" % (from_type, to_type), payload)
        for result in data.get("results", []):
            from_id = str(result.get("from", {}).get("id", ""))
            for target in result.get("to", []):
                to_id = str(target.get("toObjectId", ""))
                if to_id:
                    links[from_id].append(to_id)
    return links


def load_wins():
    """
    Manually-logged calls that never made it into the CRM. Optional file;
    absent by default. Kept separate from CRM-derived numbers throughout.
    """
    if not os.path.isfile(WINS_FILE):
        return []

    import csv
    rows = []
    with open(WINS_FILE, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not row.get("company"):
                continue
            rows.append({
                "date":     row.get("date", ""),
                "company":  row.get("company", ""),
                "contact":  row.get("contact", ""),
                "campaign": row.get("campaign", ""),
                "outcome":  row.get("outcome", ""),
                "note":     row.get("note", ""),
            })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def fetch_deals(deal_ids):
    deals = {}
    for batch in chunked(sorted(set(deal_ids)), 100):
        payload = {
            "properties": DEAL_PROPERTIES,
            "inputs": [{"id": did} for did in batch],
        }
        data = api_post("/crm/v3/objects/deals/batch/read", payload)
        for item in data.get("results", []):
            deals[str(item.get("id"))] = item.get("properties", {})
    return deals


# ── Dates ────────────────────────────────────────────────────────────────────

def parse_ts(value):
    """HubSpot hands back ISO-8601; normalise to a Manila-local datetime."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MANILA_TZ)


def week_start(dt):
    """
    Report weeks run Wednesday→Tuesday, matching the BD weekly report so the two
    can be read side by side. Returns the Wednesday that opens the week.
    """
    if dt is None:
        return None
    days_since_wed = (dt.weekday() - 2) % 7          # Monday=0, so Wednesday=2
    start = (dt - timedelta(days=days_since_wed))
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def week_key(dt):
    start = week_start(dt)
    return start.strftime("%Y-%m-%d") if start else None


def week_label(key):
    start = datetime.strptime(key, "%Y-%m-%d")
    end = start + timedelta(days=6)
    if start.month == end.month:
        return "%d–%d %s" % (start.day, end.day, end.strftime("%b"))
    return "%d %s – %d %s" % (start.day, start.strftime("%b"), end.day, end.strftime("%b"))


# ── Build ────────────────────────────────────────────────────────────────────

def load_registry():
    with open(REGISTRY_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def build():
    if not HUBSPOT_TOKEN:
        sys.exit("HUBSPOT_TOKEN is not set.")

    registry  = load_registry()
    campaigns = registry["campaigns"]

    # email → (campaign, recipient record)
    lookup = {}
    for camp in campaigns:
        for rec in camp["recipients"]:
            lookup[rec["email"]] = (camp, rec)

    all_emails = sorted(lookup.keys())
    print("Registry: %d campaigns, %d unique recipients" % (len(campaigns), len(all_emails)))

    print("Fetching contacts from HubSpot…")
    contacts = fetch_contacts_by_email(all_emails)
    print("   matched %d of %d registry addresses" % (len(contacts), len(all_emails)))

    contact_ids = [str(c["id"]) for c in contacts.values()]

    # Direct attribution: deals hanging off the person we emailed.
    print("Fetching deals for %d contacts…" % len(contact_ids))
    deal_links = fetch_associations("contacts", "deals", contact_ids) if contact_ids else {}

    # Company attribution: deals hanging off their employer. This is what
    # catches a referral — we email a vendor manager, they hand us to a
    # colleague, and the deal ends up on the colleague's record instead.
    print("Fetching company-level deals…")
    company_links = fetch_associations("contacts", "companies", contact_ids) if contact_ids else {}
    company_ids = sorted({cid for ids in company_links.values() for cid in ids})
    company_deals = fetch_associations("companies", "deals", company_ids) if company_ids else {}

    direct_deal_ids = {d for ids in deal_links.values() for d in ids}
    all_deal_ids    = set(direct_deal_ids)
    for ids in company_deals.values():
        all_deal_ids.update(ids)

    deals = fetch_deals(sorted(all_deal_ids)) if all_deal_ids else {}
    print("   %d deals (%d direct, %d via company)"
          % (len(deals), len(direct_deal_ids), len(all_deal_ids) - len(direct_deal_ids)))

    # ── Per-recipient roll-up ────────────────────────────────────────────────
    now            = datetime.now(MANILA_TZ)
    per_camp       = {}
    weekly         = defaultdict(lambda: defaultdict(int))
    results        = []      # every recipient who produced a deal
    replies        = []      # every recipient who replied
    credited_deals = set()   # a deal counts once, for the first campaign to reach it

    for camp in campaigns:
        per_camp[camp["id"]] = {
            "id": camp["id"], "label": camp["label"], "sector": camp["sector"],
            "targeted": len(camp["recipients"]),
            "sent": 0, "replied": 0, "engaged": 0,
            "deals": 0, "dc_held": 0, "won": 0,
            "pipeline_value": 0.0, "won_value": 0.0,
            "first_send": None, "last_send": None,
        }

    for email, (camp, rec) in lookup.items():
        contact = contacts.get(email)
        if not contact:
            continue                                   # drafted, never sent

        props  = contact.get("properties", {}) or {}
        source = (props.get("hs_object_source_label") or "").upper()
        contacted = parse_ts(props.get("notes_last_contacted"))
        created   = parse_ts(props.get("createdate"))

        # Verified send: the record was created by email logging, or it carries
        # a "last contacted" stamp (covers people already in the CRM before we
        # wrote to them). Anything else is a CRM record we never emailed.
        if source not in LOGGED_SOURCES and contacted is None:
            continue

        # For a record email logging created, its birth IS the send. For a
        # pre-existing contact, the last-contacted stamp is the better signal.
        sent_at = created if source in LOGGED_SOURCES else contacted
        if sent_at is None:
            continue

        row = per_camp[camp["id"]]
        row["sent"] += 1
        wk = week_key(sent_at)
        if wk:
            weekly[wk]["sent"] += 1
        if row["first_send"] is None or sent_at < row["first_send"]:
            row["first_send"] = sent_at
        if row["last_send"] is None or sent_at > row["last_send"]:
            row["last_send"] = sent_at

        display_name = (rec.get("name")
                        or " ".join(x for x in [props.get("firstname"), props.get("lastname")] if x).strip()
                        or email)
        company = rec.get("company") or props.get("company") or ""

        replied_at = parse_ts(props.get("hs_sales_email_last_replied"))
        if replied_at:
            row["replied"] += 1
            rwk = week_key(replied_at)
            if rwk:
                weekly[rwk]["replied"] += 1
            replies.append({
                "name": display_name, "company": company, "email": email,
                "title": rec.get("title") or props.get("jobtitle") or "",
                "campaign": camp["label"], "campaign_id": camp["id"],
                "replied": replied_at.strftime("%Y-%m-%d"),
                "status": props.get("hs_lead_status") or "",
            })

        if (props.get("hs_lead_status") or "") in ENGAGED_STATUSES:
            row["engaged"] += 1

        # ── Deals for this recipient ─────────────────────────────────────────
        # Direct first, then anything on their employer that we have not
        # already credited. A deal is counted once, against the first
        # recipient/campaign that reaches it.
        contact_id = str(contact["id"])
        candidates = [(d, "direct") for d in deal_links.get(contact_id, [])]
        for company_id in company_links.get(contact_id, []):
            for d in company_deals.get(company_id, []):
                candidates.append((d, "company"))

        for deal_id, attribution in candidates:
            if deal_id in credited_deals:
                continue
            dprops = deals.get(deal_id)
            if not dprops:
                continue
            created_deal = parse_ts(dprops.get("createdate"))

            # A company-route deal only counts if it was created after we first
            # wrote to that company. Without this, any pre-existing or inbound
            # deal at a large employer (Capital One, Aflac, Humana) would be
            # credited to outbound purely because we emailed someone there.
            # A direct association to the person we emailed needs no such guard.
            if attribution == "company":
                if created_deal is None or created_deal < sent_at:
                    continue

            credited_deals.add(deal_id)
            stage = dprops.get("dealstage") or ""
            try:
                amount = float(dprops.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0

            row["deals"] += 1
            dwk = week_key(created_deal)
            if dwk:
                weekly[dwk]["deals"] += 1

            attended = stage in DC_ATTENDED_STAGES
            won      = stage == STAGES["closed_won"]
            if attended:
                row["dc_held"] += 1
                if dwk:
                    weekly[dwk]["dc_held"] += 1
            if won:
                row["won"] += 1
                row["won_value"] += amount
            elif stage not in DEAD_STAGES:
                row["pipeline_value"] += amount

            results.append({
                "name": display_name, "company": company, "email": email,
                "campaign": camp["label"], "campaign_id": camp["id"],
                "sector": camp["sector"],
                "deal": dprops.get("dealname") or "",
                "deal_id": deal_id,
                "stage": STAGE_LABELS.get(stage, stage),
                "stage_id": stage,
                "attribution": attribution,
                "in_bd_pipeline": (dprops.get("pipeline") == BD_PIPELINE_ID),
                "amount": amount,
                "created": created_deal.strftime("%Y-%m-%d") if created_deal else "",
                "dc_held": attended,
                "won": won,
                "dead": stage in DEAD_STAGES,
            })

    # ── Trend series: last N Wed→Tue weeks ───────────────────────────────────
    current_week = week_start(now)
    trend = []
    for i in range(TREND_WEEKS - 1, -1, -1):
        start = current_week - timedelta(days=7 * i)
        key   = start.strftime("%Y-%m-%d")
        bucket = weekly.get(key, {})
        trend.append({
            "week":    key,
            "label":   week_label(key),
            "sent":    bucket.get("sent", 0),
            "replied": bucket.get("replied", 0),
            "deals":   bucket.get("deals", 0),
            "dc_held": bucket.get("dc_held", 0),
        })

    # ── Totals ───────────────────────────────────────────────────────────────
    camp_rows = [c for c in per_camp.values() if c["sent"] > 0]
    for row in camp_rows:
        row["first_send"] = row["first_send"].strftime("%Y-%m-%d") if row["first_send"] else ""
        row["last_send"]  = row["last_send"].strftime("%Y-%m-%d") if row["last_send"] else ""
        row["reply_rate"] = round(100.0 * row["replied"] / row["sent"], 1) if row["sent"] else 0.0
        row["deal_rate"]  = round(100.0 * row["deals"] / row["sent"], 1) if row["sent"] else 0.0
    camp_rows.sort(key=lambda r: (r["deals"], r["replied"], r["sent"]), reverse=True)

    never_sent = sum(c["targeted"] for c in per_camp.values()) - sum(r["sent"] for r in camp_rows)

    totals = {
        "campaigns_live": len(camp_rows),
        "targeted":       sum(c["targeted"] for c in per_camp.values()),
        "sent":           sum(r["sent"] for r in camp_rows),
        "never_sent":     never_sent,
        "replied":        sum(r["replied"] for r in camp_rows),
        "engaged":        sum(r["engaged"] for r in camp_rows),
        "deals":          sum(r["deals"] for r in camp_rows),
        "dc_held":        sum(r["dc_held"] for r in camp_rows),
        "won":            sum(r["won"] for r in camp_rows),
        "pipeline_value": round(sum(r["pipeline_value"] for r in camp_rows), 2),
        "won_value":      round(sum(r["won_value"] for r in camp_rows), 2),
    }
    totals["reply_rate"] = round(100.0 * totals["replied"] / totals["sent"], 1) if totals["sent"] else 0.0
    totals["deal_rate"]  = round(100.0 * totals["deals"] / totals["sent"], 1) if totals["sent"] else 0.0
    totals["dc_rate"]    = round(100.0 * totals["dc_held"] / totals["sent"], 1) if totals["sent"] else 0.0
    totals["reply_to_deal"] = (round(100.0 * totals["deals"] / totals["replied"], 1)
                               if totals["replied"] else 0.0)

    # ── Sector roll-up ───────────────────────────────────────────────────────
    sectors = defaultdict(lambda: {"sent": 0, "replied": 0, "deals": 0, "dc_held": 0, "won": 0})
    for row in camp_rows:
        s = sectors[row["sector"]]
        for k in ("sent", "replied", "deals", "dc_held", "won"):
            s[k] += row[k]
    sector_rows = []
    for name, vals in sectors.items():
        vals = dict(vals, sector=name)
        vals["reply_rate"] = round(100.0 * vals["replied"] / vals["sent"], 1) if vals["sent"] else 0.0
        vals["deal_rate"]  = round(100.0 * vals["deals"] / vals["sent"], 1) if vals["sent"] else 0.0
        sector_rows.append(vals)
    sector_rows.sort(key=lambda r: (r["deals"], r["sent"]), reverse=True)

    results.sort(key=lambda r: (r["won"], r["dc_held"], r["created"]), reverse=True)
    replies.sort(key=lambda r: r["replied"], reverse=True)

    wins = load_wins()

    data = {
        "generated":  now.strftime("%Y-%m-%d %H:%M") + " Manila",
        "week_label": week_label(current_week.strftime("%Y-%m-%d")),
        "portal_id":  PORTAL_ID,
        "totals":     totals,
        "campaigns":  camp_rows,
        "sectors":    sector_rows,
        "trend":      trend,
        "results":    results,
        "replies":    replies[:200],
        "wins":       wins,
        # Surfaced on the page so the numbers are never read without the
        # conditions that produced them.
        "notes": {
            "reply_caveat": ("Replies count any inbound message on the thread, "
                             "including out-of-office and bounce notifications. "
                             "Treat as an upper bound."),
            "crm_linked_deals": len(results),
            "manual_wins": len(wins),
        },
    }

    with open(DATA_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    print("Wrote %s" % DATA_FILE)

    inject_into_html(data)

    print("\nSent %(sent)d · replies %(replied)d (%(reply_rate)s%%) · deals %(deals)d "
          "· DCs held %(dc_held)d · won %(won)d" % totals)
    return data


def inject_into_html(data):
    """
    The page reads a JSON blob embedded between markers, so the report works
    when opened straight off disk as well as over GitHub Pages.
    """
    if not os.path.isfile(HTML_FILE):
        print("! %s not found — skipping HTML injection" % HTML_FILE)
        return

    with open(HTML_FILE, encoding="utf-8") as fh:
        html = fh.read()

    start = "/* DATA-START */"
    end   = "/* DATA-END */"
    payload = "%s\nconst REPORT = %s;\n%s" % (start, json.dumps(data, ensure_ascii=False), end)

    pattern = re.compile(re.escape(start) + ".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(html):
        print("! data markers not found in %s — skipping injection" % HTML_FILE)
        return

    with open(HTML_FILE, "w", encoding="utf-8") as fh:
        fh.write(pattern.sub(lambda _: payload, html, count=1))
    print("Patched %s" % HTML_FILE)


if __name__ == "__main__":
    build()
