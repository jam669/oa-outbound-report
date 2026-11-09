"""Build the "New Product: AI Automation Outreach" tab data.

Reads the AI campaign contact log (local only; it is not in this repo), the wave
metadata in ai_waves.json and the hand-kept reply log in ai_replies.csv, then
writes the JSON between /* AI-DATA-START */ and /* AI-DATA-END */ in
outbound-report.html. The weekly Action only rewrites the DATA-START block, so
this tab is untouched by it; re-run this script after each new wave or reply.

Privacy: this repo is public, so only company, wave, track, date and status are
published. No names, titles or email addresses.

    python build_ai_tab.py [path/to/_CONTACT-LOG.csv]
"""
import csv, json, os, re, sys, collections, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.normpath(os.path.join(HERE, "..", "..", "01_Campaigns", "AI-Automation", "_CONTACT-LOG.csv"))
HTML_FILE = os.path.join(HERE, "outbound-report.html")
START, END = "/* AI-DATA-START */", "/* AI-DATA-END */"

TRACKS = {
    "warm (past bd deal)": "Warm: past BD deal",
    "cold trigger: back-office hire": "Back-office job post",
    "cold trigger: ai/automation hire": "AI / automation job post",
    "cv add-on": "CV-list add-on",
    "new-ai-leader": "New AI leader",
    "new-ops-leader": "New ops / finance leader",
}


def main():
    log = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG
    meta = json.load(open(os.path.join(HERE, "ai_waves.json"), encoding="utf-8"))
    rows = list(csv.DictReader(open(log, encoding="utf-8-sig")))
    replies = list(csv.DictReader(open(os.path.join(HERE, "ai_replies.csv"), encoding="utf-8-sig")))
    rep_by_co = {r["company"].strip().lower(): r for r in replies}

    sends, seen = [], set()
    for r in rows:
        key = (r["email"].lower(), r.get("touch", "1"))
        if key in seen:
            continue
        seen.add(key)
        co = r["company"].strip()
        rep = rep_by_co.get(co.lower())
        status = "sent"
        if rep:
            status = {"auto": "auto-reply", "bounce": "bounced"}.get(rep["type"], "replied-" + rep["type"])
        sends.append({
            "company": co, "wave": r["wave"], "touch": int(r.get("touch") or 1),
            "track": TRACKS.get(r["track"].strip().lower(), r["track"].strip()),
            "date": r["sent_at"][:10], "status": status,
        })

    first = [s for s in sends if s["touch"] == 1]
    by_wave = collections.OrderedDict()
    for w in meta["waves"]:
        by_wave[w["id"]] = dict(w, sent=0, auto=0, replied=0, bounced=0)
    for s in first:
        w = by_wave.setdefault(s["wave"], {"id": s["wave"], "date": s["date"], "trigger": "", "angle": "", "offer": "",
                                           "sent": 0, "auto": 0, "replied": 0, "bounced": 0})
        w["sent"] += 1
        if s["status"] == "auto-reply": w["auto"] += 1
        elif s["status"] == "bounced": w["bounced"] += 1
        elif s["status"].startswith("replied"): w["replied"] += 1

    track_counts = collections.Counter(s["track"] for s in first)
    human = [r for r in replies if r["type"] not in ("auto", "bounce")]
    positive = [r for r in replies if r["type"] == "positive"]
    scans = sum(1 for r in replies if r["type"] == "scan")

    data = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + " Manila",
        "campaign": meta["campaign"], "launched": meta["launched"],
        "product": meta["product"], "offer": meta["offer"], "gates": meta["gates"],
        "pilot_targets": meta["pilot_targets"], "funnel": meta["funnel"], "followups": meta["followups"],
        "totals": {
            "emails": len(sends), "first_touches": len(first),
            "companies": len({s["company"].lower() for s in first}),
            "waves": len([w for w in by_wave.values() if w["sent"]]),
            "human_replies": len(human), "positive": len(positive) + scans, "scans": scans,
            "auto": sum(1 for r in replies if r["type"] == "auto"),
            "bounced": sum(1 for r in replies if r["type"] == "bounce"),
        },
        "waves": [w for w in by_wave.values() if w["sent"]],
        "tracks": [{"track": k, "sent": v} for k, v in track_counts.most_common()],
        "replies": [{k: r[k] for k in ("company", "wave", "type", "date", "note")} for r in replies],
        "sends": sorted(first, key=lambda s: (s["wave"], s["company"].lower())),
    }

    html = open(HTML_FILE, encoding="utf-8").read()
    payload = START + "\nconst AI_REPORT = " + json.dumps(data, ensure_ascii=False) + ";\n" + END
    pattern = re.compile(re.escape(START) + ".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(html):
        sys.exit("AI-DATA markers not found in outbound-report.html")
    with open(HTML_FILE, "w", encoding="utf-8") as fh:
        fh.write(pattern.sub(lambda _: payload, html, count=1))
    print("AI tab: %d emails, %d companies, %d waves, %d human replies" %
          (len(sends), data["totals"]["companies"], data["totals"]["waves"], len(human)))


if __name__ == "__main__":
    main()
