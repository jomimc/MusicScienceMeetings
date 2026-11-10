"""HTML -> compact text for LLM extraction (the 'v6' recipe tested on 2026-11-10).

Steps: drop scripts/styles/images; keep the main content (fall back to <body>
minus header/nav/footer when <main> is tiny, e.g. Google Sites); put a '---'
line before repeated sibling blocks (one per listing record); keep links as
[label](url) except calendar-export and very long links; cut lines at 200 chars.
See docs/extract_test_smpc.md and docs/easy_sources.md for why each step exists.
"""
import re

from bs4 import BeautifulSoup


def _clean(html):
    s = BeautifulSoup(html, "lxml")
    for t in s(["script", "style", "noscript", "svg", "iframe", "template", "img"]):
        t.decompose()
    return s


def _main_node(s):
    m = s.find("main") or s.find(attrs={"role": "main"})
    full = len((s.body or s).get_text(strip=True))
    if m and len(m.get_text(strip=True)) >= 0.2 * full:
        return m
    body = s.body or s
    for t in body(["header", "nav", "footer", "aside"]):
        t.decompose()
    return body


def _mark_records(node, min_repeats=4):
    for parent in node.find_all(True):
        groups = {}
        for ch in parent.find_all(True, recursive=False):
            if ch.get("class"):
                groups.setdefault((ch.name, ch["class"][0]), []).append(ch)
        for kids in groups.values():
            if len(kids) >= min_repeats:
                for ch in kids:
                    ch.insert_before(BeautifulSoup("<span>---</span>", "lxml").span)
    return node


def _links_to_text(node):
    for a in node.find_all("a", href=True):
        href, label = a["href"], a.get_text(" ", strip=True)
        if href.startswith(("#", "javascript:", "mailto:")) or not label:
            continue
        a.replace_with(f"[{label}]({href})")
    return re.sub(r"\n{2,}", "\n", node.get_text("\n", strip=True))


def _drop_noise_links(txt):
    def repl(m):
        label, url = m.group(1), m.group(2)
        if label in ("Google Calendar", "ICS"):
            return ""
        return label if len(url) > 120 else m.group(0)
    # no newlines inside label/url, so a cut link can't swallow following text
    txt = re.sub(r"\[([^\]\n]*)\]\(([^)\s]*)\)", repl, txt)
    return re.sub(r"\n{2,}", "\n", txt)


def _cut_lines(txt, n=200):
    return "\n".join(l if len(l) <= n else l[:n] + "…" for l in txt.split("\n"))


def html_to_text(html):
    node = _mark_records(_main_node(_clean(html)))
    return _cut_lines(_drop_noise_links(_links_to_text(node)))
