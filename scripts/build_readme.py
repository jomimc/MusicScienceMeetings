"""Generate data/README.md from data/events.yaml and data/sources.yaml.

Usage: python3 scripts/build_readme.py [TODAY as YYYY-MM-DD]
"""
import re
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
today = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
events = yaml.safe_load((ROOT / "data/events.yaml").read_text())
sources = yaml.safe_load((ROOT / "data/sources.yaml").read_text())


def fmt_dates(e):
    s, en = e.get("start"), e.get("end")
    if not s:
        return "TBA"
    return s if not en or en == s else f"{s} – {en}"


def link(text, url):
    text = text.replace("|", "/")
    return f"[{text}]({url})" if url else text


for e in events:  # same link rule as the site: event url, else first URL in source
    if not e.get("url"):
        m = re.search(r"https?://\S+", e.get("source") or "")
        if m:
            e["url"] = m.group(0)


def next_deadline(e):
    open_ = [d for d in e.get("deadlines", []) if d.get("date") and d["date"] >= today]
    return min(open_, key=lambda d: d["date"]) if open_ else None


def mark(e):
    return "" if e.get("verified") else " ⚠️"


upcoming = [e for e in events if e.get("start") and (e.get("end") or e["start"]) >= today]
announced = [e for e in events if not e.get("start") and e.get("status") != "past"]
past = [e for e in events if e not in upcoming and e not in announced]

L = ["# Music Science Meetings — data", "",
     f"Generated from `events.yaml` and `sources.yaml` on {today} by `scripts/build_readme.py`. "
     "Do not edit by hand.", "",
     "⚠️ = reported by a web search, not yet checked against the official page.", "",
     "## Upcoming and ongoing", "",
     "| Dates | Meeting | Location | Next deadline |", "|---|---|---|---|"]
for e in sorted(upcoming, key=lambda e: e["start"]):
    nd = next_deadline(e)
    nds = f"{nd['date']} ({nd['type'].replace('_', ' ')})" if nd else ""
    L.append(f"| {fmt_dates(e)} | {link(e['name'], e.get('url'))}{mark(e)} | {e.get('location', '')} | {nds} |")

L += ["", "## Open deadlines", "", "| Deadline | Type | Meeting |", "|---|---|---|"]
dls = [(d, e) for e in events for d in e.get("deadlines", []) if d.get("date") and d["date"] >= today]
for d, e in sorted(dls, key=lambda x: x[0]["date"]):
    note = f" — {d['note']}" if d.get("note") else ""
    L.append(f"| {d['date']} | {d['type'].replace('_', ' ')}{note} | {link(e['name'], e.get('url'))}{mark(e)} |")

L += ["", "## Announced, dates not yet published", ""]
L += [f"- {link(e['name'], e.get('url'))}{mark(e)}" + (f" — {e['location']}" if e.get("location") else "")
      + (f" ({e['notes']})" if e.get("notes") else "") for e in announced]

L += ["", "## Past editions", "", "<details><summary>Show</summary>", ""]
L += [f"- {fmt_dates(e)} — {link(e['name'], e.get('url'))}{mark(e)}" + (f", {e['location']}" if e.get("location") else "")
      for e in sorted(past, key=lambda e: e.get("start") or "0000", reverse=True)]
L += ["", "</details>", "", "## Sources", "",
      "| Source | Kind | Topics | Read by | Status |", "|---|---|---|---|---|"]
for s in sorted(sources, key=lambda s: (s["kind"], s["name"].lower())):
    L.append(f"| {link(s['name'], s.get('url'))} | {s['kind'].replace('_', ' ')} | {', '.join(s.get('topics', []))} "
             f"| {s['method']} | {s['status']} |")

(ROOT / "data/README.md").write_text("\n".join(L) + "\n")
print(f"data/README.md: {len(upcoming)} upcoming, {len(dls)} open deadlines, {len(announced)} announced, "
      f"{len(past)} past, {len(sources)} sources")
