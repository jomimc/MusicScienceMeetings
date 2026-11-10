// Render event cards from window.EVENTS; sort by date / next deadline; filter by tags.
(function () {
  const EVENTS = window.EVENTS || [];
  const today = new Date().toISOString().slice(0, 10);
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const state = { sort: "date", past: false, tags: new Set() };

  const parts = (iso) => { const [y, m, d] = iso.split("-").map(Number); return { y, m, d }; };
  const fmt = (iso) => { const p = parts(iso); return `${p.d} ${MONTHS[p.m - 1]} ${p.y}`; };

  function fmtRange(s, e) {
    if (!s) return "Dates TBA";
    if (!e || e === s) return fmt(s);
    const a = parts(s), b = parts(e);
    if (a.y === b.y && a.m === b.m) return `${a.d}–${b.d} ${MONTHS[a.m - 1]} ${a.y}`;
    if (a.y === b.y) return `${a.d} ${MONTHS[a.m - 1]} – ${b.d} ${MONTHS[b.m - 1]} ${a.y}`;
    return `${fmt(s)} – ${fmt(e)}`;
  }

  const isPast = (ev) => ev.status === "past" || (ev.start && (ev.end || ev.start) < today);
  const nextDeadline = (ev) =>
    ev.deadlines.filter((d) => d.date >= today).sort((a, b) => a.date.localeCompare(b.date))[0] || null;

  function daysUntil(iso) {
    const ms = Date.parse(iso + "T00:00:00Z") - Date.parse(today + "T00:00:00Z");
    return Math.round(ms / 86400000);
  }

  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v; else n.setAttribute(k, v);
    }
    for (const k of kids) if (k != null) n.append(k);
    return n;
  }

  function card(ev) {
    const title = ev.url ? el("a", { href: ev.url, rel: "noopener" }, ev.name) : ev.name;
    const fmtShown = ev.format && !(ev.location || "").toLowerCase().includes(ev.format) ? ev.format : null;
    const meta = [fmtRange(ev.start, ev.end), ev.location, fmtShown].filter(Boolean).join(" · ");
    const nd = nextDeadline(ev);
    let dl = null;
    if (nd) {
      const n = daysUntil(nd.date);
      const when = n === 0 ? "today" : n === 1 ? "tomorrow" : `in ${n} days`;
      dl = el("p", { class: "deadline" + (n <= 14 ? " soon" : "") },
        `Next: ${nd.type.replace(/_/g, " ")} ${fmt(nd.date)} (${when})`);
    }
    const badges = el("div", { class: "badges" });
    if (!isPast(ev) && ev.start && ev.start <= today) badges.append(el("span", { class: "badge live" }, "happening now"));
    if (isPast(ev)) badges.append(el("span", { class: "badge" }, "past"));
    if (!ev.verified) badges.append(el("span", { class: "badge warn", title: "Found by web search; not yet checked against the official page" }, "⚠ unverified"));
    const tags = el("div", { class: "card-tags" }, ...ev.tags.map((t) => el("span", { class: "chip small" }, t)));
    return el("article", { class: "card" + (isPast(ev) ? " is-past" : "") },
      el("h2", {}, title), el("p", { class: "meta" }, meta),
      ev.series.length ? el("p", { class: "series" }, ev.series.join(" · ")) : null,
      dl, badges.childElementCount ? badges : null, tags);
  }

  function sortKey(ev) {
    if (state.sort === "deadline") {
      const nd = nextDeadline(ev);
      return (nd ? "0" + nd.date : "1") + (ev.start || "9999");
    }
    return ev.start || "9999";
  }

  function render() {
    let list = EVENTS.filter((ev) => state.past || !isPast(ev));
    if (state.tags.size) list = list.filter((ev) => ev.tags.some((t) => state.tags.has(t)));
    list.sort((a, b) => sortKey(a).localeCompare(sortKey(b)));
    if (state.past && state.sort === "date") list.reverse();
    const box = document.getElementById("cards");
    box.replaceChildren(...list.map(card));
    if (!list.length) box.append(el("p", { class: "empty" }, "No meetings match."));
    document.getElementById("count").textContent = `${list.length} meeting${list.length === 1 ? "" : "s"}`;
    document.getElementById("clear-tags").hidden = !state.tags.size;
    for (const b of document.querySelectorAll("#tags button"))
      b.classList.toggle("on", state.tags.has(b.dataset.tag));
  }

  // tag buttons, most common first
  const counts = {};
  for (const ev of EVENTS) for (const t of ev.tags) counts[t] = (counts[t] || 0) + 1;
  const tagBox = document.getElementById("tags");
  for (const t of Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b))) {
    const b = el("button", { type: "button", class: "chip", "data-tag": t }, t);
    b.addEventListener("click", () => { state.tags.has(t) ? state.tags.delete(t) : state.tags.add(t); render(); });
    tagBox.append(b);
  }
  document.getElementById("clear-tags").addEventListener("click", () => { state.tags.clear(); render(); });
  for (const b of document.querySelectorAll("[data-sort]"))
    b.addEventListener("click", () => {
      state.sort = b.dataset.sort;
      for (const x of document.querySelectorAll("[data-sort]")) x.classList.toggle("on", x === b);
      render();
    });
  document.getElementById("show-past").addEventListener("change", (e) => { state.past = e.target.checked; render(); });
  if (window.GENERATED) document.getElementById("generated").textContent = `Updated ${fmt(window.GENERATED)}.`;
  render();
})();
