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

REPLIES — WHY HUBSPOT IS NOT USED
--------------------------------
`hs_sales_email_last_replied` is unusable and is NOT reported as a reply here.
Mailsuite (the send tracker) emails a "your email has not been opened yet"
reminder into the thread 24h after each send, and HubSpot logs that as a reply on
the contact. Measured across 1,894 contacts: of 881 "replies", 426 landed at
almost exactly +24h and only 2 fell in the 1-23h window where genuine replies
would sit. The field correlates with prospects NOT reading the email.

Genuine replies come from `connected_email` in weekly.json — the EOW's own
"Connected To" count, which is human-verified and excludes auto-replies,
out-of-office and bounces. The HubSpot figure is still carried as
`thread_notifications` so it stays traceable, but is never shown as engagement.

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

import email.utils
import imaplib
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

# The weekly update as reported to leadership each Friday. Its outreach figures
# are authoritative: they count emails sent across every campaign, including the
# ones whose sendlists were never saved to the workspace.
WEEKLY_FILE = os.path.join(HERE, "weekly.json")

# Weeks shown in the trend series (report weeks run Wed→Tue, matching the BD report).
TREND_WEEKS = 12

# ── Outreach counts from Apps Script ─────────────────────────────────────────
# App passwords are disabled on the Workspace account, so IMAP is unavailable.
# OutreachCounts.gs runs inside the tracker workbook as Jam, counts sent mail
# with GmailApp (no admin approval needed), and publishes a CSV:
#     week_start,sends,people
# Set OUTREACH_CSV_URL to that published URL. Absent, the report falls back to
# HubSpot's people-reached count and says so on the page.
OUTREACH_CSV_URL = os.environ.get("OUTREACH_CSV_URL", "")

# ── Gmail over IMAP (fallback path) ──────────────────────────────────────────
# The EOW report counts emails SENT. Two things make Gmail the only source that
# can match it:
#   * several waves were built in-session and never saved to the workspace, so a
#     registry built from local files cannot see them at all;
#   * HubSpot's logged contacts mix in the whole team's outreach, and owner id
#     does not identify who actually sent the email.
# Gmail's Sent folder is exactly "what Jam sent", which is the EOW definition.
#
# Optional: without credentials the report still runs, and falls back to
# counting people reached via HubSpot (a lower number — follow-up touches and
# unsaved campaigns are invisible to it).
GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_HOST     = "imap.gmail.com"
GMAIL_SENT_BOX = '"[Gmail]/Sent Mail"'

# Recipients that are colleagues, not prospects — excluded from outreach counts.
INTERNAL_DOMAINS = {"outsourceaccelerator.com"}

# Automation and logging addresses that ride along on real sends.
IGNORED_RECIPIENTS = {"bcc.hubspot.com"}

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
    # Total logged touches on this contact. The EOW report counts emails SENT
    # (a 3rd follow-up is a 3rd send); people-reached alone undercounts that.
    "num_contacted_notes",
]

DEAL_PROPERTIES = [
    "dealname", "dealstage", "pipeline", "amount", "createdate",
    "closedate", "hubspot_owner_id", "lead_source",
]

# Lead Source that marks a deal as belonging to this campaign. Set on the deal
# in HubSpot; it is Jam's explicit call on provenance and therefore outranks any
# attribution this script could infer. Deals without it are listed separately
# rather than counted — that is what keeps unrelated work (Thriviae, Deltabit)
# out of a vendor-campaign report even when the company association would match.
CAMPAIGN_LEAD_SOURCE = "vendor campaign"

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


def fetch_outreach_csv():
    """
    Weekly send counts published by OutreachCounts.gs.

    Returns {week_start: {"sends": n, "people": n}} or None when not configured
    or unreachable — None means "not measured", which the page distinguishes
    from a measured zero.
    """
    if not OUTREACH_CSV_URL:
        return None

    try:
        resp = requests.get(OUTREACH_CSV_URL, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print("   ! outreach CSV unreachable (%s)" % exc)
        return None

    import csv as _csv
    counts = {}
    for row in _csv.DictReader(resp.text.splitlines()):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        week = row.get("week_start")
        if not week:
            continue
        try:
            counts[week] = {"sends": int(row.get("sends") or 0),
                            "people": int(row.get("people") or 0)}
        except ValueError:
            continue

    if not counts:
        print("   ! outreach CSV had no usable rows")
        return None
    return counts


def fetch_gmail_sends(since):
    """
    Every external address we emailed from the Sent folder since `since`.

    Returns [(sent_at, recipient_email), ...] — one entry per recipient per
    message, which is the EOW definition of an outreach send: a follow-up to
    the same person on Thursday is a second send, not a duplicate.

    Returns None (not an empty list) when no credentials are configured, so the
    caller can tell "not measured" apart from "measured, and it was zero".
    """
    if not (GMAIL_USER and GMAIL_PASSWORD):
        return None

    sends = []
    box = None
    try:
        box = imaplib.IMAP4_SSL(GMAIL_HOST)
        box.login(GMAIL_USER, GMAIL_PASSWORD)
        box.select(GMAIL_SENT_BOX, readonly=True)

        # IMAP wants DD-Mon-YYYY and matches on the server's date, so this is a
        # slightly generous window; exact bucketing happens off the Date header.
        status, data = box.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK":
            print("   ! Gmail search failed (%s)" % status)
            return None

        ids = data[0].split()
        print("   %d sent messages to scan" % len(ids))

        # Fetch headers in blocks; pulling bodies would be needlessly heavy.
        for block in chunked(ids, 200):
            id_set = b",".join(block).decode()
            status, chunk = box.fetch(id_set, "(BODY.PEEK[HEADER.FIELDS (TO CC DATE)])")
            if status != "OK":
                continue
            for part in chunk:
                if not isinstance(part, tuple):
                    continue
                header = part[1].decode("utf-8", errors="ignore")

                date_match = re.search(r"^Date:\s*(.+)$", header, re.M | re.I)
                sent_at = None
                if date_match:
                    try:
                        sent_at = email.utils.parsedate_to_datetime(date_match.group(1).strip())
                    except (TypeError, ValueError):
                        sent_at = None
                if sent_at is None:
                    continue
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                sent_at = sent_at.astimezone(MANILA_TZ)

                recipients = []
                for field in ("To", "Cc"):
                    m = re.search(r"^%s:\s*(.+?)(?=^\S+:|\Z)" % field, header, re.M | re.I | re.S)
                    if m:
                        recipients += [addr for _, addr in
                                       email.utils.getaddresses([m.group(1).replace("\r\n", " ")])]

                for addr in recipients:
                    addr = (addr or "").strip().lower()
                    if not addr or "@" not in addr:
                        continue
                    domain = addr.rsplit("@", 1)[-1]
                    if domain in INTERNAL_DOMAINS or domain in IGNORED_RECIPIENTS:
                        continue
                    sends.append((sent_at, addr))

    except (imaplib.IMAP4.error, OSError) as exc:
        print("   ! Gmail unavailable (%s) — falling back to HubSpot counts" % exc)
        return None
    finally:
        if box is not None:
            try:
                box.logout()
            except Exception:
                pass

    return sends


def load_weekly():
    """The EOW updates, newest first. Absent file → no weekly panel."""
    if not os.path.isfile(WEEKLY_FILE):
        return []
    try:
        with open(WEEKLY_FILE, encoding="utf-8") as fh:
            weeks = json.load(fh).get("weeks", [])
    except (OSError, ValueError) as exc:
        print("   ! weekly.json unreadable (%s)" % exc)
        return []
    weeks = [w for w in weeks if w.get("week")]
    weeks.sort(key=lambda w: w["week"], reverse=True)
    return weeks


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
    Report weeks run Monday→Sunday, so they contain the EOW's Mon–Sat window
    exactly. (They used to run Wed→Tue to match the BD report, which split every
    EOW week across two buckets and made the two impossible to reconcile.)
    Returns the Monday that opens the week.
    """
    if dt is None:
        return None
    start = dt - timedelta(days=dt.weekday())        # Monday = 0
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
    #
    # Degrades rather than dies: this route needs a companies read scope the
    # token may not carry. Losing referral attribution is a worse report;
    # losing the whole report because of it is worse still.
    company_links, company_deals = {}, {}
    if contact_ids:
        print("Fetching company-level deals…")
        try:
            company_links = fetch_associations("contacts", "companies", contact_ids)
            company_ids = sorted({cid for ids in company_links.values() for cid in ids})
            company_deals = fetch_associations("companies", "deals", company_ids) if company_ids else {}
        except RuntimeError as exc:
            print("   ! company attribution unavailable, continuing with direct only")
            print("     (%s)" % str(exc)[:180])
            company_links, company_deals = {}, {}

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
    excluded_deals = []      # matched a contact/employer but not tagged Vendor Campaign

    for camp in campaigns:
        per_camp[camp["id"]] = {
            "id": camp["id"], "label": camp["label"], "sector": camp["sector"],
            "targeted": len(camp["recipients"]),
            "sent": 0, "touches": 0, "followups": 0, "replied": 0, "engaged": 0,
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

        # Touches = emails actually sent, which is what the EOW report counts.
        # HubSpot exposes the total per contact plus the first and latest touch
        # dates, so totals are exact; weekly buckets place the first and latest
        # touch precisely and can only blur touches in between.
        try:
            touches = int(props.get("num_contacted_notes") or 1)
        except (TypeError, ValueError):
            touches = 1
        touches = max(touches, 1)
        row["touches"] += touches
        if wk:
            weekly[wk]["touches"] += 1
        if contacted is not None and touches > 1:
            lwk = week_key(contacted)
            if lwk:
                weekly[lwk]["touches"] += 1
                weekly[lwk]["followups"] += 1
            row["followups"] += touches - 1
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

            # Lead Source is the deciding vote. A deal that reached a DC through
            # this campaign is tagged "Vendor Campaign" in HubSpot; anything else
            # is other work that merely shares a contact or an employer, and it
            # is recorded for review rather than counted.
            lead_source = (dprops.get("lead_source") or "").strip()
            if lead_source.lower() != CAMPAIGN_LEAD_SOURCE:
                credited_deals.add(deal_id)
                excluded_deals.append({
                    "deal":        dprops.get("dealname") or "",
                    "deal_id":     deal_id,
                    "company":     company or rec.get("company") or "",
                    "lead_source": lead_source or "(not set)",
                    "stage":       STAGE_LABELS.get(dprops.get("dealstage") or "",
                                                    dprops.get("dealstage") or ""),
                    "created":     created_deal.strftime("%Y-%m-%d") if created_deal else "",
                    "reason":      ("Lead Source is '%s', not 'Vendor Campaign'" % lead_source
                                    if lead_source else "Lead Source is not set"),
                })
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
                "lead_source": lead_source,
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
            "touches": bucket.get("touches", 0),
            "replied": bucket.get("replied", 0),
            "deals":   bucket.get("deals", 0),
            "dc_held": bucket.get("dc_held", 0),
        })

    # ── Totals ───────────────────────────────────────────────────────────────
    camp_rows = [c for c in per_camp.values() if c["sent"] > 0]
    for row in camp_rows:
        row["first_send"] = row["first_send"].strftime("%Y-%m-%d") if row["first_send"] else ""
        row["last_send"]  = row["last_send"].strftime("%Y-%m-%d") if row["last_send"] else ""
        # Kept for the campaign table but renamed: this is thread activity from
        # HubSpot, dominated by Mailsuite notifications, not prospect replies.
        row["notifications"] = row.pop("replied", 0)
        row["deal_rate"]  = round(100.0 * row["deals"] / row["sent"], 1) if row["sent"] else 0.0
    camp_rows.sort(key=lambda r: (r["deals"], r["sent"]), reverse=True)

    never_sent = sum(c["targeted"] for c in per_camp.values()) - sum(r["sent"] for r in camp_rows)

    totals = {
        "campaigns_live": len(camp_rows),
        "targeted":       sum(c["targeted"] for c in per_camp.values()),
        "sent":           sum(r["sent"] for r in camp_rows),
        "touches":        sum(r["touches"] for r in camp_rows),
        "followups":      sum(r["followups"] for r in camp_rows),
        "never_sent":     never_sent,
        # Thread activity, not replies. Reported only so the number is
        # traceable; the page does not present it as engagement.
        "thread_notifications": sum(r["notifications"] for r in camp_rows),
        "engaged":        sum(r["engaged"] for r in camp_rows),
        "deals":          sum(r["deals"] for r in camp_rows),
        "dc_held":        sum(r["dc_held"] for r in camp_rows),
        "won":            sum(r["won"] for r in camp_rows),
        "pipeline_value": round(sum(r["pipeline_value"] for r in camp_rows), 2),
        "won_value":      round(sum(r["won_value"] for r in camp_rows), 2),
    }
    totals["deal_rate"] = round(100.0 * totals["deals"] / totals["sent"], 1) if totals["sent"] else 0.0
    totals["dc_rate"]   = round(100.0 * totals["dc_held"] / totals["sent"], 1) if totals["sent"] else 0.0

    # ── Sector roll-up ───────────────────────────────────────────────────────
    sectors = defaultdict(lambda: {"sent": 0, "notifications": 0, "deals": 0, "dc_held": 0, "won": 0})
    for row in camp_rows:
        s = sectors[row["sector"]]
        for k in ("sent", "notifications", "deals", "dc_held", "won"):
            s[k] += row[k]
    sector_rows = []
    for name, vals in sectors.items():
        vals = dict(vals, sector=name)
    
        vals["deal_rate"]  = round(100.0 * vals["deals"] / vals["sent"], 1) if vals["sent"] else 0.0
        sector_rows.append(vals)
    sector_rows.sort(key=lambda r: (r["deals"], r["sent"]), reverse=True)

    results.sort(key=lambda r: (r["won"], r["dc_held"], r["created"]), reverse=True)
    replies.sort(key=lambda r: r["replied"], reverse=True)

    # ── Gmail: outreach actually sent ────────────────────────────────────────
    # Counted separately from the HubSpot roll-up above, and reconciled against
    # it on the page, because the two answer different questions:
    #   HubSpot "reached"  = distinct people from a known campaign list
    #   Gmail   "outreach" = emails sent, including follow-ups and any campaign
    #                        whose list was never saved to the workspace
    print("Counting outreach sent…")
    window_start = current_week - timedelta(days=7 * (TREND_WEEKS - 1))

    outreach = None

    # Best source: the EOW's own numbers. They are what leadership already reads,
    # they count sends rather than people, and they cover campaigns whose lists
    # never reached the workspace.
    weekly_updates = load_weekly()
    totals_outreach = sum(int(w.get("outreach") or 0) for w in weekly_updates)
    if weekly_updates:
        reported = {w["week"]: w for w in weekly_updates}
        total = 0
        for row in trend:
            hit = reported.get(row["week"])
            row["outreach"] = int(hit.get("outreach") or 0) if hit else 0
            total += row["outreach"]
        latest = weekly_updates[0]
        outreach = {
            "total":        total,
            "attributed":   None,
            "unattributed": None,
            "this_week":    int(latest.get("outreach") or 0),
            "top_unknown":  [],
            "source":       "weekly update (EOW)",
        }
        print("   %d sends across %d reported weeks (EOW)" % (total, len(weekly_updates)))

    # Otherwise: weekly counts published by the Apps Script (works where IMAP
    # cannot, because the Workspace disables app passwords).
    weekly_counts = None if outreach else fetch_outreach_csv()
    if weekly_counts is not None:
        total = 0
        for row in trend:
            row["outreach"] = weekly_counts.get(row["week"], {}).get("sends", 0)
            total += row["outreach"]
        outreach = {
            "total":        total,
            "attributed":   None,      # the sheet counts sends, not identities
            "unattributed": None,
            "this_week":    weekly_counts.get(current_week.strftime("%Y-%m-%d"), {}).get("sends", 0),
            "top_unknown":  [],
            "source":       "Apps Script (Gmail)",
        }
        print("   %d sends across %d weeks (Apps Script)" % (total, len(trend)))

    gmail_sends = None if outreach else fetch_gmail_sends(window_start)

    if outreach:
        pass
    elif gmail_sends is None:
        print("   not configured — outreach counts fall back to HubSpot")
    else:
        by_week = defaultdict(int)
        by_week_known = defaultdict(int)
        attributed = unattributed = 0
        unknown_domains = defaultdict(int)

        for sent_at, addr in gmail_sends:
            wk = week_key(sent_at)
            if not wk:
                continue
            by_week[wk] += 1
            if addr in lookup:
                attributed += 1
                by_week_known[wk] += 1
            else:
                unattributed += 1
                unknown_domains[addr.rsplit("@", 1)[-1]] += 1

        for row in trend:
            row["outreach"] = by_week.get(row["week"], 0)

        outreach = {
            "total":        len(gmail_sends),
            "attributed":   attributed,
            "unattributed": unattributed,
            "this_week":    by_week.get(current_week.strftime("%Y-%m-%d"), 0),
            # Biggest senders with no campaign list on disk — these are the
            # waves that need their sendlist committed to be broken out by name.
            "top_unknown":  sorted(unknown_domains.items(), key=lambda kv: -kv[1])[:15],
        }
        print("   %d sends · %d matched to a campaign · %d from lists not on disk"
              % (outreach["total"], attributed, unattributed))

    # Genuine human replies come from the EOW, never from HubSpot. HubSpot's
    # hs_sales_email_last_replied logs Mailsuite's "not opened yet" reminders as
    # replies — 426 of 881 landed exactly 24h after a send — so it measures the
    # opposite of engagement and is deliberately not reported as a reply count.
    connected = sum(int(w.get("connected_email") or 0) for w in weekly_updates)
    totals["outreach"] = totals_outreach
    totals["connected"] = connected
    totals["connected_rate"] = (round(100.0 * connected / totals_outreach, 2)
                                if totals_outreach else 0.0)

    for row in trend:
        hit = next((w for w in weekly_updates if w["week"] == row["week"]), None)
        row["connected"] = int(hit.get("connected_email") or 0) if hit else 0
        row.pop("replied", None)

    wins = load_wins()

    data = {
        "generated":  now.strftime("%Y-%m-%d %H:%M") + " Manila",
        "week_label": week_label(current_week.strftime("%Y-%m-%d")),
        "portal_id":  PORTAL_ID,
        "totals":     totals,
        "campaigns":  camp_rows,
        "sectors":    sector_rows,
        "trend":      trend,
        "outreach":   outreach,
        "weekly":     weekly_updates,
        "results":    results,
        "excluded_deals": excluded_deals,
        "replies":    [],
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

    print("\nOutreach %d emails to %d people · connected %d (%.2f%%) · deals %d · DCs held %d"
          % (totals_outreach, totals["sent"], totals["connected"], totals["connected_rate"], totals["deals"], totals["dc_held"]))
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
