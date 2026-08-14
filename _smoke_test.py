"""
Smoke test — renders outbound-report.html against synthetic data in a stubbed DOM.

Catches runtime errors in the page script (undefined refs, bad branches) without
needing a browser. Run after editing the HTML:

    python _smoke_test.py

Not part of the weekly job; safe to delete.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "outbound-report.html")

SCENARIOS = {
    "populated": {
        "generated": "2026-08-14 21:00 Manila",
        "week_label": "12–18 Aug",
        "portal_id": "44390857",
        "totals": {
            "campaigns_live": 3, "targeted": 268, "sent": 234, "never_sent": 34,
            "replied": 96, "engaged": 0, "deals": 2, "dc_held": 1, "won": 1,
            "pipeline_value": 600.0, "won_value": 1200.0,
            "reply_rate": 41.0, "deal_rate": 0.9, "dc_rate": 0.4, "reply_to_deal": 2.1,
        },
        "campaigns": [
            {"id": "vendor-manager-icp", "label": "Vendor-Manager ICP", "sector": "Vendor Managers",
             "targeted": 168, "sent": 150, "replied": 60, "engaged": 0, "deals": 2, "dc_held": 1,
             "won": 1, "pipeline_value": 600.0, "won_value": 1200.0, "reply_rate": 40.0,
             "deal_rate": 1.3, "first_send": "2026-06-10", "last_send": "2026-08-12"},
            {"id": "wave-01-vendor-managers", "label": "Vendor Leads (New)", "sector": "Vendor Managers",
             "targeted": 50, "sent": 50, "replied": 20, "engaged": 0, "deals": 0, "dc_held": 0,
             "won": 0, "pipeline_value": 0.0, "won_value": 0.0, "reply_rate": 40.0,
             "deal_rate": 0.0, "first_send": "2026-07-09", "last_send": "2026-07-09"},
        ],
        "sectors": [{"sector": "Vendor Managers", "sent": 234, "replied": 96,
                     "deals": 2, "dc_held": 1, "won": 1, "reply_rate": 41.0, "deal_rate": 0.9}],
        "trend": [
            {"week": "2026-06-10", "label": "10–16 Jun", "sent": 120, "replied": 30, "deals": 0, "dc_held": 0},
            {"week": "2026-07-08", "label": "8–14 Jul", "sent": 100, "replied": 40, "deals": 1, "dc_held": 1},
            {"week": "2026-08-12", "label": "12–18 Aug", "sent": 14, "replied": 26, "deals": 1, "dc_held": 0},
        ],
        "results": [
            {"name": "Mark Bailey", "company": "Neighbor", "email": "mark@neighbor.com",
             "campaign": "Vendor-Manager ICP", "campaign_id": "vendor-manager-icp",
             "sector": "Vendor Managers", "deal": "Neighbor - Sales", "deal_id": "1",
             "stage": "DC Completed", "stage_id": "222405237", "attribution": "company",
             "in_bd_pipeline": True, "amount": 600.0, "created": "2026-08-11",
             "dc_held": True, "won": False, "dead": False},
        ],
        "replies": [
            {"name": "Mark Bailey", "company": "Neighbor", "email": "mark@neighbor.com",
             "title": "Head of Ops", "campaign": "Vendor-Manager ICP",
             "campaign_id": "vendor-manager-icp", "replied": "2026-08-13", "status": ""},
        ],
        "wins": [
            {"date": "2026-08-04", "company": "Humana", "contact": "Matt Rowley",
             "campaign": "Vendor-Manager ICP", "outcome": "DC held",
             "note": "referred by vendor-manager contact"},
        ],
        "notes": {"reply_caveat": "upper bound", "crm_linked_deals": 1, "manual_wins": 1},
    },
    # The state the report is actually in today: reach and replies, nothing logged downstream.
    "no_conversions": {
        "generated": "2026-08-14 21:00 Manila",
        "week_label": "12–18 Aug",
        "portal_id": "44390857",
        "totals": {
            "campaigns_live": 3, "targeted": 268, "sent": 234, "never_sent": 34,
            "replied": 96, "engaged": 0, "deals": 0, "dc_held": 0, "won": 0,
            "pipeline_value": 0.0, "won_value": 0.0,
            "reply_rate": 41.0, "deal_rate": 0.0, "dc_rate": 0.0, "reply_to_deal": 0.0,
        },
        "campaigns": [
            {"id": "wave-01-vendor-managers", "label": "Vendor Leads (New)", "sector": "Vendor Managers",
             "targeted": 50, "sent": 50, "replied": 20, "engaged": 0, "deals": 0, "dc_held": 0,
             "won": 0, "pipeline_value": 0.0, "won_value": 0.0, "reply_rate": 40.0,
             "deal_rate": 0.0, "first_send": "2026-07-09", "last_send": "2026-07-09"},
        ],
        "sectors": [], "trend": [
            {"week": "2026-08-12", "label": "12–18 Aug", "sent": 14, "replied": 26, "deals": 0, "dc_held": 0},
        ],
        "results": [], "replies": [], "wins": [],
        "notes": {"reply_caveat": "upper bound", "crm_linked_deals": 0, "manual_wins": 0},
    },
    "empty": None,
}

DOM_STUB = r"""
const captured = {};
function el(id) {
  return {
    id,
    _html: "",
    hidden: false,
    style: {},
    dataset: {},
    set innerHTML(v) { this._html = v; captured[id] = v; },
    get innerHTML() { return this._html; },
    set textContent(v) { captured[id] = v; },
    setAttribute() {}, addEventListener() {},
    querySelectorAll() { return []; },
  };
}
const nodes = {};
global.document = {
  getElementById: (id) => (nodes[id] = nodes[id] || el(id)),
};
global.window = { innerWidth: 1280 };
"""


def render(name, payload):
    with open(HTML, encoding="utf-8") as fh:
        html = fh.read()

    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    data_line = "const REPORT = %s;" % ("null" if payload is None else json.dumps(payload))

    js = DOM_STUB + "\n" + data_line + "\n" + script + """
if (!captured.app) { console.error("FAIL: #app never rendered"); process.exit(1); }
console.log("  app html: " + captured.app.length + " chars");
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name

    try:
        proc = subprocess.run([node_bin(), path], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)

    ok = proc.returncode == 0
    print(("  PASS " if ok else "  FAIL ") + name)
    if proc.stdout.strip():
        print("   " + proc.stdout.strip().replace("\n", "\n   "))
    if not ok:
        print("   " + (proc.stderr.strip()[:900] or "(no stderr)"))
    return ok


def node_bin():
    return "node"


if __name__ == "__main__":
    print("Rendering scenarios against %s\n" % os.path.basename(HTML))
    results = [render(name, payload) for name, payload in SCENARIOS.items()]
    print("\n%d/%d scenarios rendered" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)
