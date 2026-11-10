"""Turn an 'Add a meeting' or 'Correct a meeting' issue into an edit of data/events.yaml.

  new event : parse the form, fetch the official page, Haiku cross-check (page wins, form fills
              gaps), then merge with the same rules as the weekly updater (scripts/update.py).
  correction: find the event by its ID (or name), apply only fields the submitter changed, and
              mark those fields `locked` so the weekly updater won't overwrite a human fix.
Nothing is committed here; the workflow (.github/workflows/intake.yml) opens a pull request.
Issue text is untrusted: it only reaches the LLM as data, and YAML via safe_dump.

Usage: python3 scripts/intake.py --issue N [--report FILE] [--dry-run] [--today YYYY-MM-DD]
       python3 scripts/intake.py --issue-file issue.json ...   (local testing; GitHub issue JSON)
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
import update  # noqa: E402
from textprep import html_to_text  # noqa: E402

ROOT = update.ROOT
REPO = os.environ.get("GITHUB_REPOSITORY", "jomimc/MusicScienceMeetings")
EMPTY = {"", "_No response_", "None"}


def load_issue(args):
    if args.issue_file:
        return json.loads(Path(args.issue_file).read_text())
    out = subprocess.run(["gh", "api", f"repos/{REPO}/issues/{args.issue}"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def parse_form(body, template):
    """Issue forms render as '### <label>\\n\\n<value>'; map labels back to field ids."""
    spec = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE" / template).read_text())
    by_label = {b["attributes"]["label"].strip(): b["id"] for b in spec["body"] if b.get("id")}
    fields = {}
    for label, value in re.findall(r"^###\s+(.+?)\s*\n(.*?)(?=^###\s|\Z)", body or "", re.S | re.M):
        fid = by_label.get(label.strip())
        value = value.strip()
        if fid and value not in EMPTY:
            fields[fid] = value
    return fields


def parse_dates(s):
    found = re.findall(r"20\d\d-\d\d-\d\d", s or "")
    return (found[0], found[-1]) if found else (None, None)


def parse_deadlines(s):
    out = []
    for line in (s or "").splitlines():
        m = re.match(r"\s*([A-Za-z _-]+):\s*(\S+)\s*(?:\((.*)\))?", line)
        if not m:
            continue
        t = m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        d = update.valid_date(m.group(2))
        e = {"type": t if t in llm.DL_TYPES else "other", "date": d, "note": (m.group(3) or "").strip() or None}
        if e["type"] == "other" and t not in llm.DL_TYPES:
            e["note"] = (m.group(1).strip() + (f" — {e['note']}" if e["note"] else ""))
        out.append({k: v for k, v in e.items() if v is not None})
    return out


def guess_series(run, name, url):
    """Attach to a tracked conference series if the website host or the series id/acronym matches."""
    host = update.norm_url(url).split("/")[0]
    toks = update.toks(name)
    for s in run.sources:
        if s["kind"] != "conference_series":
            continue
        hosts = {update.norm_url(s.get(k, "")).split("/")[0] for k in ("url", "feed", "url_pattern") if s.get(k)}
        if (host and host in hosts) or s["id"] in toks or update.similar(s["name"], name):
            return s["id"]
    return "user-submissions"


def handle_new(run, f, issue, L):
    url, page_text = f.get("url"), None
    if url:
        code, html, err = update.fetch(url)
        if html:
            page_text = html_to_text(html)
        L.append(f"- Official page: <{url}> — " + ("fetched" if page_text else f"could not be fetched ({err})"))
    form_text = "\n".join(f"{k}: {v}" for k, v in f.items())
    out = None
    if not run.args.dry_run:
        out, err = llm.extract_submission(run.client, page_text or "", url or "", form_text, run.today)
        if err:
            L.append(f"- LLM error: {err}")
    if not out:  # dry run or LLM failure: use the form as typed
        start, end = parse_dates(f.get("dates"))
        out = {"name": f.get("name"), "start_date": start, "end_date": end, "location": f.get("location"),
               "format": f.get("format") if f.get("format") in ("in-person", "online", "hybrid") else None,
               "url": url, "deadlines": [], "is_event_page": False}
    series = guess_series(run, out.get("name") or f.get("name", ""), url)
    c = run.candidate(out, series, url or f"https://github.com/{REPO}/issues/{issue['number']}")
    c["url"] = c["url"] or url
    c["source"] = url if page_text else f"https://github.com/{REPO}/issues/{issue['number']}"
    before = {e["id"] for e in run.events}
    run.merge(c)
    new_ids = [e["id"] for e in run.events if e["id"] not in before]
    verified = bool(page_text) and bool(out.get("is_event_page"))
    for e in run.events:
        if e["id"] in new_ids:
            e["verified"] = verified
            e["notes"] = f"Submitted via issue #{issue['number']} by @{issue['user']['login']}." + (
                "" if verified else " Official page not readable by script; details as submitted.")
            if f.get("topics"):
                e["notes"] += f" Suggested topics: {f['topics'][:100]}"
    if new_ids:
        e = next(e for e in run.events if e["id"] == new_ids[0])
        L.append(f"- **Added** `{e['id']}`: {e['name']} — {e.get('start') or 'dates TBA'}"
                 + (f" — {e['location']}" if e.get("location") else "") + f" — series `{series}`"
                 + ("" if verified else " — ⚠ unverified (page not readable)"))
    elif run.log["changed"]:
        L.append("- Already listed; existing entry updated:")
        for eid, name, ch, src in run.log["changed"]:
            L += [f"  - `{eid}` {k}: {o if o is not None else '∅'} → {n}" for k, o, n in ch]
    else:
        L.append("- Already listed (or in the past); nothing to change.")
    if run.log["conflicts"]:
        L += [f"- Conflict, not applied: `{x[0]}` {x[2]} listed {x[3]}, submission says {x[4]}" for x in run.log["conflicts"]]
    if f.get("recurring", "").startswith("yes") and series == "user-submissions":
        L.append("- Submitter says this is a recurring series: consider adding it to `data/sources.yaml`.")


def handle_correction(run, f, issue, L):
    e = next((x for x in run.events if x["id"] == f.get("event_id")), None)
    if e is None and f.get("name"):
        e = next((x for x in run.events if update.similar(x["name"], f["name"], 0.5)), None)
    if e is None:
        L.append(f"- Could not find the event (`{f.get('event_id')}` / {f.get('name')!r}); needs manual handling.")
        return
    changes = []
    start, end = parse_dates(f.get("dates"))
    proposed = {"name": f.get("name"), "start": start, "end": end, "location": f.get("location"),
                "format": (f.get("event_format") or "").lower() or None, "url": f.get("url")}
    if proposed["format"] not in (None, "in-person", "online", "hybrid"):
        proposed["format"] = None
    for k, v in proposed.items():
        if v and v != e.get(k):
            changes.append((k, e.get(k), v))
            e[k] = v
    if "deadlines" in f:
        new_dl = parse_deadlines(f["deadlines"])
        key = lambda ds: sorted((d.get("type"), d.get("date") or "", d.get("note") or "") for d in ds)
        if key(new_dl) != key(e.get("deadlines", [])):
            changes.append(("deadlines", "; ".join(f"{d['type']} {d.get('date', '?')}" for d in e.get("deadlines", [])) or "∅",
                            "; ".join(f"{d['type']} {d.get('date', '?')}" for d in new_dl) or "∅"))
            e["deadlines"] = new_dl
    if changes:
        locked = e.get("locked")
        if locked is not True:
            e["locked"] = sorted(set(locked or []) | {k for k, _, _ in changes})  # protect the human fix
        e["checked"] = run.today
        if f.get("evidence", "").startswith("http"):
            e["source"], e["verified"] = f["evidence"], True
    note = f"Correction via issue #{issue['number']} by @{issue['user']['login']}" + (f": {f['comment'][:200]}" if f.get("comment") else ".")
    e["notes"] = (e.get("notes", "") + " " + note).strip()
    L.append(f"- Event `{e['id']}`: {e['name']}")
    L += [f"  - {k}: {o if o not in (None, '') else '∅'} → {n}" for k, o, n in changes] or ["  - no field changes (comment recorded in notes)"]
    if changes:
        L.append(f"  - Changed fields are now `locked`, so the weekly updater won't overwrite them.")
    if f.get("comment") and re.search(r"cancel|remove|delete|duplicate|wrong event|not a (meeting|conference)", f["comment"], re.I):
        L.append(f"  - ⚠ Comment asks for removal or flags a duplicate: please handle by hand before merging.")
    if f.get("evidence"):
        L.append(f"  - Evidence: <{f['evidence']}>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", type=int)
    ap.add_argument("--issue-file")
    ap.add_argument("--report")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args()
    issue = load_issue(args)
    labels = {l["name"] for l in issue.get("labels", [])}
    kind = "new" if "new event" in labels else "correction" if "correction" in labels else None
    if kind is None:
        sys.exit(f"issue #{issue['number']} has neither 'new event' nor 'correction' label; nothing to do")
    run = update.Run(argparse.Namespace(dry_run=args.dry_run, force=True, only=None, report=None, today=args.today))
    f = parse_form(issue.get("body"), "new-event.yml" if kind == "new" else "correction.yml")
    L = [f"## {'New meeting' if kind == 'new' else 'Correction'} from #{issue['number']} (@{issue['user']['login']})", ""]
    (handle_new if kind == "new" else handle_correction)(run, f, issue, L)
    if not args.dry_run:
        u = llm.USAGE
        L += ["", f"LLM: {u['calls']} call(s), {u['input']:,} + {u['output']:,} tokens, ~${llm.cost():.4f}"]
        (ROOT / "data/events.yaml").write_text(update.EVENTS_HEADER + "\n" + yaml.safe_dump(
            sorted(run.events, key=lambda e: (e.get("start") or "9999", e["id"])),
            sort_keys=False, allow_unicode=True, width=110))
    L += ["", f"Closes #{issue['number']}"]
    rep = "\n".join(L) + "\n"
    if args.report:
        Path(args.report).write_text(rep)
    print(rep)


if __name__ == "__main__":
    main()
