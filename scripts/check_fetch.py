"""Check which URLs a plain script can fetch (as the GitHub Actions pipeline would).

Tests every source url / feed / url_pattern (this year and next) in
data/sources.yaml and every event url in data/events.yaml with a simple GET.
No bot-protection workarounds. Prints a table; exit code 0 always.
Usage: python3 scripts/check_fetch.py [--events]   (--events also checks event URLs)
"""
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (compatible; MusicScienceMeetings fetch check)"
year = date.today().year


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(200000)
            ctype = r.headers.get("Content-Type", "").split(";")[0]
            note = "cloudflare challenge" if b"Just a moment" in body[:5000] else ""
            return str(r.status), ctype, note
    except urllib.error.HTTPError as e:
        return str(e.code), "", "blocked" if e.code in (403, 406, 429) else ""
    except Exception as e:  # DNS, TLS, timeout
        return "000", "", type(e).__name__


targets = []
for s in yaml.safe_load((ROOT / "data/sources.yaml").read_text()):
    if s.get("method") == "manual":
        continue
    for key in ("url", "feed"):
        if s.get(key):
            for y in ((year, year + 1) if "{year}" in s[key] else (None,)):
                targets.append((s["id"], key, s[key].replace("{year}", str(y)) if y else s[key]))
    if s.get("url_pattern"):
        for y in (year, year + 1):
            targets.append((s["id"], f"pattern {y}", s["url_pattern"].replace("{year}", str(y))))
if "--events" in sys.argv:
    for e in yaml.safe_load((ROOT / "data/events.yaml").read_text()):
        if e.get("url") and e.get("status") != "past":
            targets.append((e["id"], "event url", e["url"]))

with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(lambda t: fetch(t[2]), targets))

bad = 0
for (sid, kind, url), (code, ctype, note) in zip(targets, results):
    ok = code.startswith("2") and not note
    bad += not ok
    print(f"{'ok ' if ok else 'BAD'} {code:>3} {sid:28.28s} {kind:11.11s} {ctype:22.22s} {note:20.20s} {url}")
print(f"\n{len(targets) - bad}/{len(targets)} fetchable")
