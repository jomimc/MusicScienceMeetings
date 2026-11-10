"""Weekly update: read every automatable source, extract events, merge into data/events.yaml.

Per source `method` (data/sources.yaml):
  ical             parse feed, no LLM
  squarespace_json <url>?format=json for names/dates; Haiku on excerpts for location/deadlines
  page             page text -> Haiku list extraction
  page_follow      as page, then each event's own page -> Haiku detail extraction (+1 more_info hop)
  url_pattern      try {year} = this year and next -> Haiku detail extraction
  rss              feed items about meetings/calls -> their pages -> Haiku detail extraction
  manual / list_archive / bluesky: skipped (manual, or planned for v2)
Pages whose cleaned text is unchanged since the last run (data/state.json) are not
sent to the LLM again. Merging never deletes events; it fills empty fields, updates
changed dates/deadlines, and never touches events/fields marked `locked`.

Usage:
  python3 scripts/update.py [--dry-run] [--force] [--only id1,id2] [--report FILE] [--today YYYY-MM-DD]
    --dry-run  fetch and diff, but no LLM calls and no file writes
    --force    ignore state.json (re-extract every page)
"""
import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
from textprep import html_to_text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (compatible; MusicScienceMeetings updater; +https://github.com/jomimc/MusicScienceMeetings)"
MAX_FOLLOW = 8          # followed links per source
DATE_RE = re.compile(r"^(20[2-3]\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
CALL_RE = re.compile(r"annual meeting|conference|congress|symposium|workshop|call for (papers|proposals|abstracts)|cfp", re.I)

EVENTS_HEADER = """# Events: one entry per edition of a meeting. MASTER FILE: hand edits are kept.
# Updated weekly by scripts/update.py (GitHub Actions -> pull request).
#
# Fields
#   id         unique slug, usually <series>-<year>
#   series     list of source ids in data/sources.yaml
#   name, type (conference | symposium | workshop | study_group | study_day | other)
#   start, end ISO dates (end inclusive); absent = not yet announced
#   location, format (in-person | online | hybrid), url (edition's own page)
#   deadlines  list of {type, date, note}; type: call_opens | abstract | paper |
#              deadline (unspecified) | notification | early_registration |
#              registration | late_registration | other. date absent = only
#              approximate (wording in note).
#   status     upcoming | ongoing | past | announced (no dates yet), as of `checked`
#   verified   true = read from the official page/feed; false = reported by a
#              web search, needs confirming
#   source     where the facts came from;  checked = date of that check
#   tags       optional topic tags; replace the series' topics on the site
#   region     optional override of the region inferred from location
#   locked     true = the updater never changes this event; or a list of
#              field names it must not change (e.g. [start, end])
#   notes      free text
"""


# ------------------------------------------------------------------ helpers
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:
        return 0, None, f"{type(e).__name__}"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40].strip("-")


GENERIC = {"the", "and", "for", "conference", "annual", "cfp", "call", "papers", "proposals", "music", "musical",
           "international", "society", "meeting", "symposium", "workshop", "congress", "study", "group", "research",
           "of", "on", "in", "at", "to", "th", "nd", "rd", "st"}


def toks(s):
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", (s or "").lower())  # "nime2027" -> "nime 2027"
    words = set(re.findall(r"[a-z0-9]{3,}|\b[a-z]{2}\b", s))
    return {w for w in words if not w.isdigit()} - GENERIC  # years and generic words don't identify a meeting - {"the", "and", "conference", "annual", "cfp", "call", "for", "papers"}


def acronym(name):
    """'Auditory Perception, Cognition and Action Meeting' -> 'apcam'."""
    words = re.findall(r"[A-Za-z]+", name or "")
    return "".join(w[0] for w in words if w.lower() not in {"and", "of", "the", "for", "on", "in", "at", "to"}).lower()


def similar(a, b, threshold=0.3):
    ta, tb = toks(a), toks(b)
    if not (ta and tb):
        return False
    if any(len(t) >= 3 and acronym(b).startswith(t) for t in ta) or any(len(t) >= 3 and acronym(a).startswith(t) for t in tb):
        return True  # "APCAM 2026" ~ "Auditory Perception, Cognition and Action Meeting"
    small = ta if len(ta) <= len(tb) else tb
    subset = (ta <= tb or tb <= ta) and len(small) >= 2  # short name inside long name, if not a single word
    return subset or ta == tb or len(ta & tb) / len(ta | tb) >= threshold


def norm_url(u):
    if not u:
        return ""
    p = urlparse(u)
    return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")).lower()


def valid_date(d):
    return d if isinstance(d, str) and DATE_RE.match(d) else None


# ------------------------------------------------------------------ run state
class Run:
    def __init__(self, args):
        self.args, self.today = args, args.today
        self.sources = yaml.safe_load((ROOT / "data/sources.yaml").read_text())
        self.events = yaml.safe_load((ROOT / "data/events.yaml").read_text()) or []
        sp = ROOT / "data/state.json"
        self.state = json.loads(sp.read_text()) if sp.exists() else {"pages": {}}
        self.client = None
        if not args.dry_run:
            import anthropic
            self.client = anthropic.Anthropic()
        self.log = {"new": [], "changed": [], "conflicts": [], "failed": [], "skipped": [], "unchanged": 0, "sent": 0}

    # page text with change detection; returns (text or None, changed: bool)
    def page_text(self, url, sid):
        code, html, err = fetch(url)
        if err:
            self.log["failed"].append((sid, url, err))
            return None, False
        text = html_to_text(html)
        h = digest(text)
        old = self.state["pages"].get(url, {}).get("hash")
        self.state["pages"][url] = {"hash": h}
        changed = self.args.force or h != old
        if not changed:
            self.log["unchanged"] += 1
        return text, changed

    def llm(self, kind, text, url, label):
        self.log["sent"] += 1
        if self.args.dry_run:
            return None
        fn = llm.extract_list if kind == "list" else llm.extract_detail
        out, err = fn(self.client, text, url, label, self.today)
        if err:
            self.log["failed"].append((label, url, f"LLM: {err}"))
        return out

    # ---------------------------------------------------------- normalisation
    def candidate(self, x, sid, page_url, source_url=None):
        start, end = valid_date(x.get("start_date") or x.get("start")), valid_date(x.get("end_date") or x.get("end"))
        if end and start and end < start:
            end = None
        url = x.get("url")
        url = urljoin(page_url, url) if url else None
        dls = []
        for d in x.get("deadlines") or []:
            dd = valid_date(d.get("date"))
            if dd and start and dd >= start:
                continue  # programme days, not deadlines
            e = {"type": d.get("type") if d.get("type") in llm.DL_TYPES else "other", "date": dd}
            note = (d.get("text") or d.get("note") or "").strip()
            if note and (e["type"] in ("other", "deadline") or not dd):
                e["note"] = note[:120]
            dls.append({k: v for k, v in e.items() if v is not None})
        return {"name": (x.get("name") or "").strip(), "start": start, "end": end,  # no end = not stated
                "location": x.get("location"), "format": x.get("format"), "url": url,
                "deadlines": dls, "series": [sid], "source": source_url or page_url}

    def is_past(self, c):
        return bool(c["start"]) and (c["end"] or c["start"]) < self.today

    # -------------------------------------------------------------- methods
    def m_ical(self, s):
        code, body, err = fetch(s["feed"])
        if err:
            self.log["failed"].append((s["id"], s["feed"], err))
            return []
        body = re.sub(r"\r?\n[ \t]", "", body)
        out = []
        for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", body, re.S):
            f = {}
            for line in block.strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    f[k.split(";")[0]] = (k, v.replace("\\,", ",").replace("\\;", ";").replace("\\n", " "))
            d = lambda k: (lambda m: f"{m[1]}-{m[2]}-{m[3]}" if m else None)(re.match(r"(\d{4})(\d{2})(\d{2})", f.get(k, ("", ""))[1]))
            start, end = d("DTSTART"), d("DTEND")
            if end and "VALUE=DATE" in f.get("DTEND", ("", ""))[0] and start and end != start:
                end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()  # exclusive all-day end
            out.append(self.candidate({"name": f.get("SUMMARY", ("", ""))[1], "start": start, "end": end,
                                       "location": f.get("LOCATION", ("", None))[1] or None,
                                       "url": f.get("URL", ("", None))[1]}, s["id"], s["feed"]))
        return out

    def m_squarespace_json(self, s):
        url = s["url"].rstrip("/") + "?format=json"
        code, body, err = fetch(url)
        if err:
            self.log["failed"].append((s["id"], url, err))
            return []
        d = json.loads(body)
        tz = ZoneInfo(d.get("website", {}).get("timeZone") or "UTC")
        items, parts = d.get("upcoming", []), []
        for i, it in enumerate(items):
            excerpt = html_to_text(it.get("excerpt") or "")
            parts.append(f"---\n[{it['title']}]({urljoin(s['url'], it.get('fullUrl', ''))})\n{excerpt}")
        text = "\n".join(parts)
        h = digest(text)
        changed = self.args.force or self.state["pages"].get(url, {}).get("hash") != h
        self.state["pages"][url] = {"hash": h}
        if not changed:
            self.log["unchanged"] += 1
            return []
        extra = self.llm("list", text, s["url"], s["name"]) or {"events": []}
        by_title = {e["name"]: e for e in extra.get("events", [])}
        out = []
        for it in items:
            ex = by_title.get(it["title"]) or next((e for e in extra.get("events", []) if similar(e["name"], it["title"])), {})
            day = lambda ms: datetime.fromtimestamp(ms / 1000, tz).date().isoformat() if ms else None
            # dates from the JSON (exact) override anything the LLM read from the excerpt
            out.append(self.candidate({**ex, "name": it["title"], "start_date": day(it.get("startDate")),
                                       "end_date": day(it.get("endDate"))}, s["id"], s["url"]))
        return out

    def follow(self, url, hint, sid, depth=0):
        text, changed = self.page_text(url, sid)
        if not text or not changed:
            return None
        out = self.llm("detail", text, url, hint)
        if not out or not out.get("is_event_page"):
            return None
        more = out.get("more_info_url")
        if depth == 0 and more and not any(d.get("date") for d in out.get("deadlines", [])):
            extra = self.follow(urljoin(url, more), hint, sid, depth=1)
            if extra:  # extra is a normalised candidate (start/end/... keys)
                out["deadlines"] = out.get("deadlines", []) + [
                    {"type": d["type"], "date": d.get("date"), "text": d.get("note", "")} for d in extra["deadlines"]]
                for k, ok in (("start", "start_date"), ("end", "end_date"), ("location", "location"), ("format", "format")):
                    if not out.get(ok) and extra.get(k):
                        out[ok] = extra[k]
        c = self.candidate(out, sid, url)
        if not c["url"] or depth == 0:
            c["url"] = c["url"] or url
        return c

    def m_page(self, s, follow_links=False):
        text, changed = self.page_text(s["url"], s["id"])
        if not text or not changed:
            return []
        out = self.llm("list", text, s["url"], s["name"]) or {"events": []}
        cands = [self.candidate(x, s["id"], s["url"]) for x in out.get("events", [])]
        if follow_links:
            n = 0
            for c, x in zip(cands, out.get("events", [])):
                if x.get("needs_followup") and c["url"] and n < MAX_FOLLOW and norm_url(c["url"]) != norm_url(s["url"]):
                    n += 1
                    det = self.follow(c["url"], f"{c['name']} ({s['name']})", s["id"])
                    if det:
                        for k in ("start", "end", "location", "format"):
                            c[k] = c[k] or det[k]
                        c["deadlines"] = c["deadlines"] or det["deadlines"]
                        c["source"] = det["source"]
        return cands

    def m_page_follow(self, s):
        return self.m_page(s, follow_links=True)

    def m_url_pattern(self, s):
        year = int(self.today[:4])
        out = []
        for y in (year, year + 1):
            url = s["url_pattern"].replace("{year}", str(y))
            c = self.follow(url, f"{s['name']} {y}", s["id"])
            if c:
                c["url"] = url
                out.append(c)
        return out

    def m_rss(self, s):
        code, body, err = fetch(s["feed"])
        if err:
            self.log["failed"].append((s["id"], s["feed"], err))
            return []
        out, n = [], 0
        for item in re.findall(r"<item>(.*?)</item>", body, re.S):
            title = re.search(r"<title>(.*?)</title>", item, re.S)
            link = re.search(r"<link>(.*?)</link>", item, re.S)
            if not (title and link) or not CALL_RE.search(title.group(1)) or n >= MAX_FOLLOW:
                continue
            n += 1
            c = self.follow(link.group(1).strip(), f"{title.group(1).strip()} ({s['name']})", s["id"])
            if c:
                out.append(c)
        return out

    # ---------------------------------------------------------------- merge
    def match(self, c):
        """Find the existing event for a candidate. Rules are tried strongest first,
        each over ALL events, so a weak match never beats a strong one elsewhere."""
        nu = norm_url(c["url"])
        ser0 = lambda e: bool(set(e.get("series", [])) & set(c["series"]))
        years = lambda name, start: set(re.findall(r"\b(20\d\d)\b", name or "")) | ({start[:4]} if start else set())
        cy = years(c["name"], c["start"])
        # different editions of a series must never match: if both sides carry a year, they must agree
        same_year = lambda e: not cy or not years(e["name"], e.get("start")) or bool(cy & years(e["name"], e.get("start")))
        ser = lambda e: ser0(e) and same_year(e)
        rules = [
            # same URL plus similar name or same start (one page can host several events, e.g. a summer school)
            lambda e: nu and nu == norm_url(e.get("url")) and same_year(e) and (similar(e["name"], c["name"]) or (c["start"] and c["start"] == e.get("start"))),
            lambda e: c["start"] and ser(e) and e.get("start") == c["start"] and similar(e["name"], c["name"], 0.15),
            lambda e: c["start"] and e.get("start") == c["start"] and similar(e["name"], c["name"]),
            lambda e: c["start"] and e.get("start") and ser(e) and e["start"][:4] == c["start"][:4] and similar(e["name"], c["name"]),
            # existing entry undated (announced): same series + similar name or a shared distinctive word
            lambda e: ser(e) and not e.get("start") and (similar(e["name"], c["name"]) or toks(e["name"]) & toks(c["name"])),
            lambda e: not c["start"] and ser(e) and similar(e["name"], c["name"], 0.5),
        ]
        for rule in rules:
            for e in self.events:
                if rule(e):
                    return e
        return None

    def merge(self, c):
        if len(c["name"]) < 4 or self.is_past(c):
            return
        e = self.match(c)
        if e is None:
            year = (c["start"] or "")[:4]
            src = next((s for s in self.sources if s["id"] == c["series"][0]), {})
            base = f"{c['series'][0]}-{year}" if year and src.get("kind") == "conference_series" else (
                f"{slug(c['name'])}-{year}" if year and year not in slug(c["name"]) else slug(c["name"]) or "event")
            eid, n = base, 2
            ids = {x["id"] for x in self.events}
            while eid in ids:
                eid, n = f"{base}-{n}", n + 1
            new = {"id": eid, "series": c["series"], "name": c["name"], "type": "conference",
                   **{k: c[k] for k in ("start", "end", "location", "format", "url") if c[k]},
                   "deadlines": c["deadlines"], "verified": True, "source": c["source"], "checked": self.today}
            self.events.append({k: v for k, v in new.items() if v not in (None, [])})
            self.log["new"].append(new)
            return
        if e.get("locked") is True:
            return
        locked = set(e.get("locked") or [])
        changes = []
        old_start = e.get("start")
        if c["start"] and old_start and abs((date.fromisoformat(c["start"]) - date.fromisoformat(old_start)).days) > 60:
            # a big jump is more likely a wrong match than a reschedule: report, don't apply
            self.log["conflicts"].append((e["id"], e["name"], "start", old_start, c["start"], c["source"]))
            return
        for k in ("start", "end"):  # dates: update when they differ
            if c[k] and c[k] != e.get(k) and k not in locked:
                changes.append((k, e.get(k), c[k]))
                e[k] = c[k]
        for k in ("location", "format", "url"):  # descriptive fields: only fill gaps
            if c[k] and not e.get(k) and k not in locked:
                changes.append((k, None, c[k]))
                e[k] = c[k]
        if "deadlines" not in locked:
            # Conservative: deadline labels vary between runs, so existing deadlines are never
            # changed. Only add dated, not-yet-passed deadlines whose date isn't listed yet;
            # a different date for a type already present is flagged for review instead.
            have_dates = {d.get("date") for d in e.get("deadlines", []) if d.get("date")}
            by_type = {}
            for d in e.get("deadlines", []):
                by_type.setdefault(d["type"], []).append(d)
            for d in c["deadlines"]:
                if not d.get("date") or d["date"] < self.today or d["date"] in have_dates:
                    continue
                same = [x for x in by_type.get(d["type"], []) if x.get("date")]
                if d["type"] not in ("deadline", "other") and same:
                    self.log["conflicts"].append((e["id"], e["name"], d["type"],
                                                  ", ".join(x["date"] for x in same), d["date"], c["source"]))
                    continue
                e.setdefault("deadlines", []).append(d)
                have_dates.add(d["date"])
                changes.append((f"deadline {d['type']}", None, d["date"] + (f" ({d['note']})" if d.get("note") else "")))
        for sid in c["series"]:
            if sid not in e.setdefault("series", []):
                e["series"].append(sid)
        if changes:  # only touch `checked` on real changes, so quiet weeks produce no diff
            e["checked"] = self.today
            e["verified"] = True
            e["source"] = c["source"]
            self.log["changed"].append((e["id"], e["name"], changes, c["source"]))

    # ---------------------------------------------------------------- main
    def run(self):
        only = set(self.args.only.split(",")) if self.args.only else None
        for s in self.sources:
            if only and s["id"] not in only:
                continue
            m = getattr(self, "m_" + s["method"], None)
            if m is None or s.get("status") == "dead":
                self.log["skipped"].append((s["id"], s["method"] if m is None else "dead"))
                continue
            print(f"[{s['method']:16s}] {s['id']}", file=sys.stderr)
            try:
                cands = m(s)
            except Exception as ex:  # one broken source must not stop the run
                self.log["failed"].append((s["id"], s.get("url"), f"{type(ex).__name__}: {ex}"))
                continue
            for c in cands:
                self.merge(c)
        for e in self.events:  # status from dates, for every event
            s, en = e.get("start"), e.get("end") or e.get("start")
            if s:
                e["status"] = "past" if en < self.today else "ongoing" if s <= self.today else "upcoming"
        self.events.sort(key=lambda e: (e.get("start") or "9999", e["id"]))

    def write(self):
        (ROOT / "data/events.yaml").write_text(EVENTS_HEADER + "\n" + yaml.safe_dump(
            self.events, sort_keys=False, allow_unicode=True, width=110))
        (ROOT / "data/state.json").write_text(json.dumps(self.state, indent=1, sort_keys=True) + "\n")

    def report(self):
        L, g = [f"## Weekly update {self.today}", ""], self.log
        L += [f"**New ({len(g['new'])})**", ""] + [
            f"- {e['name']} — {e.get('start') or 'dates TBA'}" + (f" — {e['location']}" if e.get("location") else "")
            + (f" — <{e['url']}>" if e.get("url") else "") + f" (`{e['id']}`)" for e in g["new"]] + [""]
        L += [f"**Changed ({len(g['changed'])})**", ""]
        for eid, name, ch, src in g["changed"]:
            L.append(f"- {name} (`{eid}`) — source <{src}>")
            L += [f"  - {k}: {old if old is not None else '∅'} → {new}" for k, old, new in ch]
        L += ["", f"**Deadline conflicts to check by hand ({len(g['conflicts'])})** — not applied", ""] + [
            f"- {name} (`{eid}`) {t}: listed {old}, page now says {new} — <{src}>" for eid, name, t, old, new, src in g["conflicts"]]
        L += ["", f"**Failures ({len(g['failed'])})**", ""] + [f"- `{a}` {u or ''} — {err}" for a, u, err in g["failed"]]
        L += ["", f"Pages unchanged since last run (not re-extracted): {g['unchanged']}",
              f"Pages sent to the LLM: {g['sent']}" + (" (dry run: not actually sent)" if self.args.dry_run else ""),
              f"Skipped sources (manual / not yet automated / dead): {len(g['skipped'])}"]
        if not self.args.dry_run:
            u = llm.USAGE
            L.append(f"LLM: {u['calls']} calls, {u['input']:,} input + {u['output']:,} output tokens, "
                     f"~${llm.cost():.4f} ({llm.MODEL}; estimate from list prices), {u['failed']} failed")
        return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--report")
    ap.add_argument("--today", default=date.today().isoformat())
    run = Run(ap.parse_args())
    run.run()
    if not run.args.dry_run:
        run.write()
    rep = run.report()
    if run.args.report:
        Path(run.args.report).write_text(rep)
    print(rep)


if __name__ == "__main__":
    main()
