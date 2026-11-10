// Render event cards from window.EVENTS; sort by date / next deadline; filter by
// format, region and topic. Filter state is kept in the page URL (shareable).
(function () {
  const EVENTS = window.EVENTS || [];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // Reader's local calendar date (not UTC), used only to split past/upcoming.
  const now = new Date();
  const today = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"),
                 String(now.getDate()).padStart(2, "0")].join("-");

  const state = { sort: "date", past: false };
  // Within a group values are OR'd; groups are AND'd.
  const GROUPS = {
    format: { get: (ev) => [ev.format], order: ["in-person", "online", "hybrid", "not stated"], sel: new Set() },
    region: { get: (ev) => ev.regions, order: ["Europe", "North America", "Latin America", "Asia", "Oceania",
              "Middle East & Africa", "Online only", "Not stated"], sel: new Set() },
    tags:   { get: (ev) => ev.tags, order: null, sel: new Set() },
  };

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
  const isPast = (ev) => (ev.start ? (ev.end || ev.start) < today : ev.status === "past");
  const nextDeadline = (ev) =>
    ev.deadlines.filter((d) => d.date && d.date >= today).sort((a, b) => a.date.localeCompare(b.date))[0] || null;

  // Correction link: opens the GitHub "Correct a meeting" form pre-filled with this card's data.
  const REPO = "https://github.com/jomimc/MusicScienceMeetings";
  function correctionURL(ev) {
    const dates = ev.start ? (ev.end && ev.end !== ev.start ? `${ev.start} – ${ev.end}` : ev.start) : "";
    const deadlines = ev.deadlines.map((d) =>
      `${d.type.replace(/_/g, " ")}: ${d.date || "?"}${d.note ? ` (${d.note})` : ""}`).join("\n");
    const q = new URLSearchParams({
      template: "correction.yml", title: `[Fix] ${ev.name}`, event_id: ev.id, name: ev.name, dates,
      location: ev.location || "", format: ev.format === "not stated" ? "" : ev.format, url: ev.url || "", deadlines,
    });
    return `${REPO}/issues/new?${q}`;
  }
  const PENCIL = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" ' +
    'd="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zm17.71-10.21a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';

  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) k === "class" ? (n.className = v) : n.setAttribute(k, v);
    for (const k of kids) if (k != null) n.append(k);
    return n;
  }

  function card(ev) {
    const title = ev.url ? el("a", { href: ev.url, rel: "noopener" }, ev.name) : ev.name;
    const fmtShown = ev.format !== "not stated" && !(ev.location || "").toLowerCase().includes(ev.format) ? ev.format : null;
    const meta = [fmtRange(ev.start, ev.end), ev.location, fmtShown].filter(Boolean).join(" · ");
    const nd = nextDeadline(ev);
    const series = [ev.series.join(" · "), ev.via.length ? `via ${ev.via.join(", ")}` : ""].filter(Boolean).join(" — ");
    const badges = el("div", { class: "badges" });
    if (isPast(ev)) badges.append(el("span", { class: "badge" }, "past"));
    if (!ev.verified) badges.append(el("span", { class: "badge warn", title: "Found by web search; not yet checked against the official page" }, "⚠ unverified"));
    const fix = el("a", { class: "fix", href: correctionURL(ev), target: "_blank", rel: "noopener",
      title: "Suggest a correction", "aria-label": `Suggest a correction for ${ev.name}` });
    fix.innerHTML = PENCIL;
    return el("article", { class: "card" + (isPast(ev) ? " is-past" : "") }, fix,
      el("h2", {}, title), el("p", { class: "meta" }, meta),
      series ? el("p", { class: "series" }, series) : null,
      nd ? el("p", { class: "deadline" }, nd.type === "deadline" ? `Next deadline: ${fmt(nd.date)}`
        : `Next deadline: ${nd.type.replace(/_/g, " ")} — ${fmt(nd.date)}`) : null,
      badges.childElementCount ? badges : null,
      el("div", { class: "card-tags" }, ...ev.tags.map((t) => el("span", { class: "chip small" }, t))));
  }

  function sortKey(ev) {
    if (state.sort === "deadline") { const nd = nextDeadline(ev); return (nd ? "0" + nd.date : "1") + (ev.start || "9999"); }
    return ev.start || "9999";
  }

  // --- URL <-> state
  function readURL() {
    const q = new URLSearchParams(location.search);
    if (q.get("sort") === "deadline") state.sort = "deadline";
    state.past = q.get("past") === "1";
    for (const [k, g] of Object.entries(GROUPS))
      for (const v of (q.get(k) || "").split(",").filter(Boolean)) g.sel.add(v);
  }
  function writeURL() {
    const q = new URLSearchParams();
    if (state.sort !== "date") q.set("sort", state.sort);
    if (state.past) q.set("past", "1");
    for (const [k, g] of Object.entries(GROUPS)) if (g.sel.size) q.set(k, [...g.sel].join(","));
    const s = q.toString().replace(/%2C/g, ",");
    history.replaceState(null, "", s ? "?" + s : location.pathname);
  }

  function render() {
    let list = EVENTS.filter((ev) => state.past || !isPast(ev));
    for (const g of Object.values(GROUPS))
      if (g.sel.size) list = list.filter((ev) => g.get(ev).some((v) => g.sel.has(v)));
    list.sort((a, b) => sortKey(a).localeCompare(sortKey(b)));
    if (state.past && state.sort === "date") list.reverse();
    const box = document.getElementById("cards");
    box.replaceChildren(...list.map(card));
    if (!list.length) box.append(el("p", { class: "empty" }, "No meetings match."));
    document.getElementById("count").textContent = `${list.length} meeting${list.length === 1 ? "" : "s"}`;
    document.getElementById("clear-filters").hidden = !Object.values(GROUPS).some((g) => g.sel.size);
    for (const b of document.querySelectorAll("[data-sort]")) b.setAttribute("aria-pressed", String(b.dataset.sort === state.sort));
    document.getElementById("show-past").checked = state.past;
    for (const dd of document.querySelectorAll(".dd")) {
      const g = GROUPS[dd.dataset.group];
      dd.querySelector(".n").textContent = g.sel.size ? `(${g.sel.size})` : "";
      dd.classList.toggle("active", g.sel.size > 0);
      for (const b of dd.querySelectorAll("button")) b.setAttribute("aria-pressed", String(g.sel.has(b.dataset.value)));
    }
    writeURL();
  }

  // --- build dropdown panels: fixed order where given, else most common first
  readURL();
  for (const dd of document.querySelectorAll(".dd")) {
    const g = GROUPS[dd.dataset.group];
    const counts = {};
    for (const ev of EVENTS) for (const v of g.get(ev)) counts[v] = (counts[v] || 0) + 1;
    const values = g.order ? g.order.filter((v) => counts[v])
      : Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
    const panel = dd.querySelector(".panel");
    for (const v of values) {
      const b = el("button", { type: "button", class: "chip", "data-value": v, "aria-pressed": "false" }, v);
      b.addEventListener("click", () => { g.sel.has(v) ? g.sel.delete(v) : g.sel.add(v); render(); });
      panel.append(b);
    }
    dd.addEventListener("toggle", () => {  // one dropdown open at a time
      if (dd.open) for (const o of document.querySelectorAll(".dd[open]")) if (o !== dd) o.open = false;
    });
  }
  document.addEventListener("click", (e) => {  // click outside closes dropdowns
    if (!e.target.closest(".dd")) for (const o of document.querySelectorAll(".dd[open]")) o.open = false;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") for (const o of document.querySelectorAll(".dd[open]")) o.open = false;
  });
  document.getElementById("clear-filters").addEventListener("click", () => {
    for (const g of Object.values(GROUPS)) g.sel.clear();
    render();
  });
  for (const b of document.querySelectorAll("[data-sort]"))
    b.addEventListener("click", () => { state.sort = b.dataset.sort; render(); });
  document.getElementById("show-past").addEventListener("change", (e) => { state.past = e.target.checked; render(); });
  if (window.GENERATED) document.getElementById("generated").textContent = `Data updated ${fmt(window.GENERATED)}.`;
  for (const n of document.querySelectorAll(".pencil-inline")) n.innerHTML = PENCIL;
  render();
})();
