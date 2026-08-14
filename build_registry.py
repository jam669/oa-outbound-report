"""
Campaign registry builder — OA Outbound Weekly Report
=====================================================
Scans the local campaign folders and emits `campaigns.json`: every person we put
on an outbound sendlist, tagged to the campaign that targeted them.

The registry answers ONE question: "which campaign does this email address belong to?"
It deliberately does NOT claim anyone was emailed — that is HubSpot's job
(a logged Gmail send creates an EXTENSION-sourced contact). See update_outbound_report.py.

Run after building a new wave:
    python build_registry.py

Output: campaigns.json (committed to the repo so the GitHub Action can read it).
"""

import csv
import json
import os
import re
import sys
from collections import OrderedDict

# ── Where the campaigns live ─────────────────────────────────────────────────
# Paths are relative to the VS Code workspace root, resolved from this file's
# location (06_Tools/oa-outbound-report/ → ../../).
HERE      = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.abspath(os.path.join(HERE, "..", ".."))

APOLLO_DIR = os.path.join(WORKSPACE, "01_Campaigns", "Apollo-Outbound")
VENDOR_DIR = os.path.join(WORKSPACE, "01_Campaigns", "Vendor-Manager-ICP")

# Waves that were built outside 01_Campaigns/ (Aug 2026 onward, still at root).
LOOSE_WAVE_DIRS = [
    os.path.join(WORKSPACE, "wave-mortgage-2026-08-13"),
    os.path.join(WORKSPACE, "wave-ria-2026-08-14"),
]

OUT_FILE = os.path.join(HERE, "campaigns.json")

# ── Scope ────────────────────────────────────────────────────────────────────
# This report covers the Vendor-Manager play only. Other Apollo waves carry a
# vendor-style ask ("Vendor+Offer" in the tab name) but target ops/finance
# personas, not vendor/procurement managers — they are a different motion and
# would blur the numbers.
#
# To widen the report later, add campaign ids here and re-run this script;
# nothing else needs to change.
INCLUDE_CAMPAIGNS = {
    "wave-01-vendor-managers",
    "wave-02-vendor-managers",
    "vendor-manager-icp",
}

# Where the Apps Script tab name is unhelpful out of context, name it plainly —
# this report is read by people who were not in the wave build.
LABEL_OVERRIDES = {
    "wave-01-vendor-managers": "Wave 01 — Vendor Managers",
    "wave-02-vendor-managers": "Wave 02 — Vendor Managers",
}

# ── Sector map ───────────────────────────────────────────────────────────────
# Folder-name fragment → the sector shown in the report's campaign breakdown.
# The wave LABEL comes from the Apps Script tab name where one exists (that is
# the authoritative description of what the list actually was — several folder
# names drifted from their contents), else from the folder name.
SECTORS = [
    ("vendor-manager",        "Vendor Managers"),
    ("rcm",                   "Healthcare — RCM"),
    ("patient-access",        "Healthcare — Patient Access"),
    ("prior-auth",            "Healthcare — Prior Auth"),
    ("credentialing",         "Healthcare — Credentialing"),
    ("insurance-verification","Healthcare — Insurance Verification"),
    ("loan-ops",              "Financial Services — Loan Ops"),
    ("mortgage",              "Financial Services — Mortgage"),
    ("ria",                   "Financial Services — RIA / Wealth"),
    ("order-to-cash-ap",      "Finance Ops — AP & O2C"),
    ("accounting",            "Accounting & Tax"),
    ("contact-center",        "CX & Contact Centre"),
    ("payroll-hr",            "Payroll & HR"),
    ("tech-support",          "Tech & Customer Support"),
    ("eng-dev",               "Software Engineering"),
]


def sector_for(folder_name):
    for frag, sector in SECTORS:
        if frag in folder_name:
            return sector
    return "Other"


def tab_label(folder):
    """Authoritative campaign label from the Apps Script tab name, if present."""
    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".gs.txt"):
            continue
        try:
            with open(os.path.join(folder, fn), encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        m = re.search(r"_TAB\s*=\s*['\"]([^'\"]+)['\"]", text)
        if m:
            return m.group(1).replace("Apollo ", "").strip()
    return None


def pretty_folder(name):
    """wave-17-order-to-cash-ap → Wave 17 — Order To Cash Ap"""
    m = re.match(r"wave-(\d+[a-z]?)-(.+)$", name)
    if m:
        num, rest = m.group(1), m.group(2).replace("-", " ").title()
        return "Wave %s — %s" % (num.upper(), rest)
    m = re.match(r"wave-(.+?)-(\d{4}-\d{2}-\d{2})$", name)
    if m:
        return "%s wave (%s)" % (m.group(1).title(), m.group(2))
    return name.replace("-", " ").title()


def clean(value):
    return (value or "").strip()


def norm_email(value):
    return clean(value).lower()


def read_csv_rows(path):
    """utf-8-sig strips the BOM these exports all carry."""
    with open(path, encoding="utf-8-sig", errors="ignore", newline="") as fh:
        return list(csv.DictReader(fh))


def recipients_from_csv(path):
    """
    Two schemas exist across the campaign folders:

    LEGACY (waves 1-25):  Priority Tier, Company Name, Employees,
                          Contact Person Name, Job Title, Email Address, ...
    CURRENT (Aug 2026+):  Segment, First, Last, Title, Company, Email, Subject, Body
                          — where Segment gates who is actually on the sendlist
                            (SEND / FOLLOW-UP are recipients, HOLD is not).
    """
    out = []
    try:
        rows = read_csv_rows(path)
    except OSError:
        return out

    for row in rows:
        row = {clean(k): v for k, v in row.items() if k}

        if "Email Address" in row:                       # legacy schema
            email = norm_email(row.get("Email Address"))
            if not email:
                continue
            out.append({
                "email":   email,
                "name":    clean(row.get("Contact Person Name")),
                "company": clean(row.get("Company Name")),
                "title":   clean(row.get("Job Title")),
                "tier":    clean(row.get("Priority Tier")),
            })

        elif "Email" in row:                             # current schema
            segment = clean(row.get("Segment")).upper()
            # Only rows pulled from the sendlist on purpose are excluded.
            # Everything else is a recipient — the segment vocabulary varies by
            # wave ("SEND - COO/ops", "NEW (first touch)", "FOLLOW-UP (touch 2)"),
            # so an allowlist silently loses whole cohorts.
            if any(segment.startswith(skip) for skip in ("HOLD", "SKIP", "EXCLUDE", "DROP")):
                continue
            email = norm_email(row.get("Email"))
            if not email:
                continue
            name = " ".join(x for x in [clean(row.get("First")), clean(row.get("Last"))] if x)
            out.append({
                "email":   email,
                "name":    name,
                "company": clean(row.get("Company")),
                "title":   clean(row.get("Title")),
                "tier":    clean(row.get("Segment")),
            })

    return out


def recipients_from_emails_txt(path):
    """Bare address list — the drafted sendlist. No enrichment attached."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                email = norm_email(line)
                if email and "@" in email:
                    out.append({"email": email, "name": "", "company": "", "title": "", "tier": ""})
    except OSError:
        pass
    return out


def recipients_from_drafts(path):
    """
    `_waveNN_full_drafts.json` is the literal list handed to the Gmail draft
    builder — the closest thing on disk to "who we actually wrote to".
    Shape: [{"to": ..., "company": ..., "first": ..., "persona": ...}, ...]
    """
    out = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out

    if isinstance(data, dict):
        data = list(data.values())
    if not isinstance(data, list):
        return out

    for item in data:
        if not isinstance(item, dict):
            continue
        email = norm_email(item.get("to") or item.get("email") or item.get("Email"))
        if not email or "@" not in email:
            continue
        out.append({
            "email":   email,
            "name":    clean(item.get("first") or item.get("First")),
            "company": clean(item.get("company") or item.get("Company")),
            "title":   clean(item.get("title") or item.get("Title")),
            "tier":    clean(item.get("persona") or item.get("Segment")),
        })
    return out


def merge_recipients(*groups):
    """
    Union by email, keeping the richest record — the CSVs carry company/title,
    the .txt lists carry only an address, and a wave usually has both.
    """
    merged = OrderedDict()
    for group in groups:
        for rec in group:
            existing = merged.get(rec["email"])
            if existing is None:
                merged[rec["email"]] = rec
                continue
            for field in ("name", "company", "title", "tier"):
                if not existing.get(field) and rec.get(field):
                    existing[field] = rec[field]
    return list(merged.values())


def collect_campaign(folder, campaign_id, label, sector):
    """Every leads CSV + address list in a folder becomes one campaign."""
    if not os.path.isdir(folder):
        return None

    draft_recs, csv_recs, txt_recs = [], [], []
    for fn in sorted(os.listdir(folder)):
        full = os.path.join(folder, fn)
        if not os.path.isfile(full):
            continue
        low = fn.lower()
        # Skip working files: raw Apollo pulls, scoring output, exclusion logs.
        if low.endswith(".csv") and not any(
            skip in low for skip in ("removed", "excluded", "exclusion", "raw", "candidates")
        ):
            csv_recs += recipients_from_csv(full)
        elif low.endswith("emails.txt") or low.endswith("emails_wave2.txt"):
            txt_recs += recipients_from_emails_txt(full)
        elif low.endswith("drafts.json") and "second" not in low:
            draft_recs += recipients_from_drafts(full)

    # Drafts first: richest and closest to what was actually written.
    recipients = merge_recipients(draft_recs, csv_recs, txt_recs)
    if not recipients:
        return None

    return {
        "id":         campaign_id,
        "label":      label,
        "sector":     sector,
        "source":     os.path.relpath(folder, WORKSPACE).replace("\\", "/"),
        "recipients": recipients,
    }


def build():
    campaigns = []

    # ── Apollo waves ─────────────────────────────────────────────────────────
    if os.path.isdir(APOLLO_DIR):
        for name in sorted(os.listdir(APOLLO_DIR)):
            folder = os.path.join(APOLLO_DIR, name)
            if not os.path.isdir(folder) or not name.startswith("wave-"):
                continue
            camp = collect_campaign(
                folder,
                campaign_id=name,
                label=LABEL_OVERRIDES.get(name) or tab_label(folder) or pretty_folder(name),
                sector=sector_for(name),
            )
            if camp:
                campaigns.append(camp)

    # ── Waves still sitting at the workspace root ────────────────────────────
    for folder in LOOSE_WAVE_DIRS:
        name = os.path.basename(folder)
        camp = collect_campaign(
            folder,
            campaign_id=name,
            label=tab_label(folder) or pretty_folder(name),
            sector=sector_for(name),
        )
        if camp:
            campaigns.append(camp)

    # ── Vendor-Manager ICP campaign ──────────────────────────────────────────
    # Qualified + persona-tiered lists only; the scored/candidate files are
    # working sets that were never sent.
    vm_recs = []
    for fn in ("BPO_Vendor_Managers_Qualified.csv", "FunctionHead_Backfill_Shortlist.csv"):
        path = os.path.join(VENDOR_DIR, fn)
        if os.path.isfile(path):
            vm_recs += recipients_from_csv(path)

    tiered = os.path.join(VENDOR_DIR, "BPO_Persona_Tiered.csv")
    if os.path.isfile(tiered):
        for row in read_csv_rows(tiered):
            row = {clean(k): v for k, v in row.items() if k}
            # The tiered file carries an Action column — EXCLUDE rows were cut.
            if clean(row.get("Action")).upper().startswith("EXCLUDE"):
                continue
            email = norm_email(row.get("Email"))
            if not email:
                continue
            vm_recs.append({
                "email":   email,
                "name":    clean(row.get("Contact")),
                "company": clean(row.get("Company")),
                "title":   clean(row.get("Title")),
                "tier":    clean(row.get("PersonaTier")),
            })

    if vm_recs:
        campaigns.append({
            "id":         "vendor-manager-icp",
            "label":      "Vendor-Manager ICP (incl. Function-Head backfill)",
            "sector":     "Vendor Managers",
            "source":     "01_Campaigns/Vendor-Manager-ICP",
            "recipients": merge_recipients(vm_recs),
        })

    # Scope down BEFORE deduping — otherwise a vendor-manager contact who also
    # appears on an out-of-scope wave gets attributed to that wave and then
    # dropped entirely, silently shrinking the campaign.
    campaigns = [c for c in campaigns if c["id"] in INCLUDE_CAMPAIGNS]

    # ── First campaign wins a contested address ──────────────────────────────
    # Later waves deliberately re-touch some earlier recipients; attributing to
    # the first campaign that reached them keeps reply credit with the opener.
    seen = {}
    for camp in campaigns:
        kept = []
        for rec in camp["recipients"]:
            if rec["email"] in seen:
                continue
            seen[rec["email"]] = camp["id"]
            kept.append(rec)
        camp["recipients"] = kept

    campaigns = [c for c in campaigns if c["recipients"]]

    registry = {
        "campaigns":       campaigns,
        "total_campaigns": len(campaigns),
        "total_recipients": sum(len(c["recipients"]) for c in campaigns),
    }

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=1, ensure_ascii=False)

    print("Campaigns: %d   Unique recipients: %d"
          % (registry["total_campaigns"], registry["total_recipients"]))
    for camp in campaigns:
        print("  %-28s %-52s %4d" % (camp["id"], camp["label"][:52], len(camp["recipients"])))
    print("\nWrote %s" % OUT_FILE)
    return registry


if __name__ == "__main__":
    build()
