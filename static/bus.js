/* Bussöversikt: läget per område för trafikinformatörer (se fp/bus.py).
   Använder hjälpfunktionerna el, $ och time från index.html. */
(function () {
  "use strict";
  const REFRESH_MS = 15000;
  const VIEW_KEY = "tabellen:busskarta";
  const STATUS = { major: "Stora förseningar", minor: "Förseningar", normal: "Normalt" };
  const RANK = { major: 2, minor: 1, normal: 0 };
  const VIEWS = {
    city: { label: "Storstockholm", lat: [59.205, 59.445], lon: [17.76, 18.33] },
    county: { label: "Hela länet", lat: [58.76, 60.06], lon: [17.25, 19.15] },
  };
  const NS = "http://www.w3.org/2000/svg";
  const nf1 = new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
  const nf0 = new Intl.NumberFormat("sv-SE");

  let data = null, geo = null, timer = null, active = false, error = null;
  let open = new Set(), showNormal = false;
  let view = "city";
  try { view = localStorage.getItem(VIEW_KEY) === "county" ? "county" : "city"; } catch (e) {}

  const pct = (x) => `${Math.round(x * 100)} %`;
  const min1 = (x) => (x == null ? "–" : `${nf1.format(x)} min`);

  function svg(tag, attrs = {}, ...children) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    for (const c of children) if (c != null) n.append(c);
    return n;
  }

  /* Samma uppdelning av Stockholms kommun som fp/areas.py */
  function stockholmPart(lat, lon) {
    if (lat < 59.305 || (lon < 18.03 && lat < 59.318)) return "Söderort";
    if ((lon < 17.995 && lat >= 59.318) || lat > 59.37) return "Västerort";
    return "Stockholm innerstad";
  }

  async function load() {
    clearTimeout(timer);
    if (!active) return;
    try {
      const res = await fetch("/api/bus");
      const d = await res.json();
      if (!res.ok) error = d.error || "Kunde inte hämta bussdata.";
      else { data = d; error = null; }
    } catch (e) {
      error = "Servern svarar inte.";
    }
    if (active) render();
    timer = setTimeout(load, REFRESH_MS);
  }

  function start() {
    active = true;
    if (!geo) fetch("/static/kommuner.geojson").then((r) => r.json()).then((g) => { geo = g; if (active) render(); }).catch(() => {});
    render();
    load();
  }
  function stop() { active = false; clearTimeout(timer); }

  /* ---------- Rendering ---------- */

  function render() {
    const box = $("bus");
    if (!data) {
      box.replaceChildren(el("div", { class: "message" + (error ? " error" : ""), text: error || "Hämtar bussläget…" }));
      return;
    }
    const keepScroll = window.scrollY;
    box.replaceChildren(head(), brief(), el("div", { class: "bv-grid" }, mapBox(), areaList()), hotspots());
    window.scrollTo(0, keepScroll);
  }

  function head() {
    const t = data.totals;
    const stat = (label, value, cls = "") => el("div", { class: cls }, el("dt", { text: label }), el("dd", { text: value }));
    return el("div", { class: "bv-head" },
      el("dl", { class: "sum-stats" },
        stat("bussar i trafik", nf0.format(t.buses)),
        stat("enligt tidtabell", nf0.format(t.planned)),
        stat("minst 5 min sena", pct(t.share5), t.share5 >= 0.25 ? "short" : ""),
        stat("snittförsening", min1(t.avg)),
        stat("inställda turer", nf0.format(t.cancelled), t.cancelled >= 5 ? "short" : "")),
      el("span", { class: "bv-updated", text:
        `Realtid ${time(data.feed_timestamp)}${data.alerts_timestamp ? " · meddelanden " + time(data.alerts_timestamp) : ""}` + (error ? ` · ${error}` : "") }));
  }

  function brief() {
    const copy = el("button", { type: "button", class: "bv-copy", text: "Kopiera" });
    copy.addEventListener("click", async () => {
      const txt = data.brief.join(" ");
      try { await navigator.clipboard.writeText(txt); copy.textContent = "Kopierat"; }
      catch (e) { copy.textContent = "Kunde inte kopiera"; }
      setTimeout(() => (copy.textContent = "Kopiera"), 2000);
    });
    return el("section", { class: "bv-brief", "aria-label": "Läget just nu" },
      el("div", { class: "bv-brief-head" }, el("h2", { text: "Läget just nu" }), copy),
      ...data.brief.map((s) => el("p", { text: s })));
  }

  /* ---------- Karta ---------- */

  function project(v, width) {
    const k = Math.cos((59.35 * Math.PI) / 180);
    const w = (v.lon[1] - v.lon[0]) * k, h = v.lat[1] - v.lat[0];
    const height = Math.round((width * h) / w);
    return { width, height, X: (lon) => ((lon - v.lon[0]) * k / w) * width, Y: (lat) => ((v.lat[1] - lat) / h) * height };
  }

  function mapBox() {
    const seg = el("div", { class: "bv-seg", role: "group", "aria-label": "Kartutsnitt" },
      ...Object.entries(VIEWS).map(([key, v]) => {
        const b = el("button", { type: "button", "aria-pressed": String(view === key), text: v.label });
        b.addEventListener("click", () => { view = key; try { localStorage.setItem(VIEW_KEY, key); } catch (e) {} render(); });
        return b;
      }));
    const legend = el("div", { class: "bv-legend" },
      el("span", {}, el("i", { class: "dot ok" }), "i tid"),
      el("span", {}, el("i", { class: "dot late" }), "5+ min"),
      el("span", {}, el("i", { class: "dot very" }), "10+ min"),
      el("span", {}, el("i", { class: "ring" }), "många sena"));
    return el("div", { class: "bv-map" }, el("div", { class: "bv-map-head" }, seg, legend), geo ? mapSvg() : el("div", { class: "message", text: "Laddar karta…" }));
  }

  function mapSvg() {
    const v = VIEWS[view];
    const P = project(v, 600);
    const status = Object.fromEntries(data.areas.map((a) => [a.name, a.status]));
    const worstSthlm = ["Stockholm innerstad", "Västerort", "Söderort"].map((n) => status[n] || "normal")
      .sort((a, b) => RANK[b] - RANK[a])[0];
    const root = svg("svg", { viewBox: `0 0 ${P.width} ${P.height}`, class: "bv-svg", role: "img",
                              "aria-label": `Karta över ${v.label} med bussar och områdenas läge` });
    const clip = svg("clipPath", { id: "bv-clip" }, svg("rect", { x: 0, y: 0, width: P.width, height: P.height }));
    root.append(svg("defs", {}, clip));
    const g = svg("g", { "clip-path": "url(#bv-clip)" });
    root.append(g);

    for (const f of geo.features) {
      const name = f.properties.name;
      const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
      let d = "";
      for (const poly of polys) for (const ring of poly) {
        d += ring.map(([lon, lat], i) => `${i ? "L" : "M"}${P.X(lon).toFixed(1)} ${P.Y(lat).toFixed(1)}`).join("") + "Z";
      }
      const st = name === "Stockholm" ? worstSthlm : status[name] || "normal";
      const p = svg("path", { d, class: "bv-area " + st, "fill-rule": "evenodd" }, svg("title", {}, document.createTextNode(name)));
      p.addEventListener("click", (e) => {
        let target = name;
        if (name === "Stockholm") {
          const r = root.getBoundingClientRect();
          const x = ((e.clientX - r.left) / r.width) * P.width, y = ((e.clientY - r.top) / r.height) * P.height;
          const lon = v.lon[0] + (x / P.width) * (v.lon[1] - v.lon[0]), lat = v.lat[1] - (y / P.height) * (v.lat[1] - v.lat[0]);
          target = stockholmPart(lat, lon);
        }
        focusArea(target);
      });
      g.append(p);
    }
    // Bussar: i tid först, sena överst
    const vs = [...data.vehicles].sort((a, b) => a[2] - b[2]);
    const r = view === "city" ? 2.6 : 1.7;
    for (const [lat, lon, d] of vs) {
      if (lat < v.lat[0] || lat > v.lat[1] || lon < v.lon[0] || lon > v.lon[1]) continue;
      g.append(svg("circle", { cx: P.X(lon).toFixed(1), cy: P.Y(lat).toFixed(1), r, class: "bv-bus " + (d >= 10 ? "very" : d >= 5 ? "late" : "ok") }));
    }
    for (const h of data.hotspots) {
      const c = svg("circle", { cx: P.X(h.lon), cy: P.Y(h.lat), r: (view === "city" ? 9 : 6) + Math.min(h.late, 12) * 1.2, class: "bv-hot" },
        svg("title", {}, document.createTextNode(`${h.name}: ${h.late} sena bussar, snitt ${Math.round(h.avg)} min`)));
      g.append(c);
    }
    // Etiketter för områden med förseningar (och alla i länsvyn när det får plats)
    const labels = { ...Object.fromEntries(geo.features.map((f) => [f.properties.name, f.properties.label])),
      "Stockholm innerstad": [59.334, 18.07], "Västerort": [59.375, 17.925], "Söderort": [59.272, 18.04] };
    for (const a of data.areas) {
      const pos = labels[a.name];
      if (!pos) continue;
      const [lat, lon] = pos;
      if (lat < v.lat[0] || lat > v.lat[1] || lon < v.lon[0] || lon > v.lon[1]) continue;
      if (a.status === "normal" && view === "county" && a.buses < 40) continue;
      const t = svg("text", { x: P.X(lon), y: P.Y(lat), class: "bv-label " + a.status, "text-anchor": "middle" },
        document.createTextNode(a.name.replace("Stockholm innerstad", "Innerstaden")));
      t.addEventListener("click", () => focusArea(a.name));
      g.append(t);
    }
    return root;
  }

  function focusArea(name) {
    open.add(name);
    const a = data.areas.find((x) => x.name === name);
    if (a && a.status === "normal") showNormal = true;
    render();
    document.getElementById("bv-area-" + slug(name))?.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  const slug = (s) => s.toLowerCase().replace(/[^a-zåäö0-9]+/g, "-");

  /* ---------- Områden ---------- */

  function spark(values) {
    const w = 64, h = 18, pts = values.map((v, i) => (v == null ? null : [(i / (values.length - 1)) * w, h - Math.min(v, 0.6) / 0.6 * (h - 2) - 1]));
    let d = "", pen = false;
    for (const p of pts) { if (!p) { pen = false; continue; } d += `${pen ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`; pen = true; }
    if (!d) return el("span", { class: "muted", text: "–", title: "Historik byggs upp medan sidan är öppen" });
    return svg("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h, class: "bv-spark", "aria-hidden": "true" },
      svg("line", { x1: 0, x2: w, y1: h - 0.25 / 0.6 * (h - 2) - 1, y2: h - 0.25 / 0.6 * (h - 2) - 1, class: "ref" }),
      svg("path", { d }));
  }

  function trendText(t) {
    if (t == null || Math.abs(t) < 0.05) return null;
    return el("span", { class: "bv-trend " + (t > 0 ? "up" : "down"), title: "Jämfört med för 10–20 min sedan",
                        text: `${t > 0 ? "↑" : "↓"} ${Math.round(Math.abs(t) * 100)}` });
  }

  function areaRow(a) {
    const isOpen = open.has(a.name);
    const btn = el("button", { type: "button", class: "bv-name", "aria-expanded": String(isOpen) }, a.name);
    const toggle = () => { isOpen ? open.delete(a.name) : open.add(a.name); render(); };
    btn.addEventListener("click", toggle);
    const tr = el("tr", { class: "bv-row " + a.status + (isOpen ? " open" : ""), id: "bv-area-" + slug(a.name) },
      el("td", {}, el("span", { class: "mark " + a.status, text: STATUS[a.status] })),
      el("td", {}, btn),
      el("td", { class: "t" }, nf0.format(a.buses)),
      el("td", { class: "t" }, a.buses ? `${pct(a.share5)}` : "–", a.late5 ? el("span", { class: "sub", text: `${a.late5} bussar` }) : null),
      el("td", { class: "t hide-sm" }, min1(a.avg)),
      el("td", { class: "hide-sm" }, el("span", { class: "bv-sparkcell" }, spark(a.spark), trendText(a.trend))),
      el("td", { class: "t" }, a.delay_alerts ? String(a.delay_alerts) : "–"),
      el("td", { class: "t" }, a.cancelled ? String(a.cancelled) : "–"));
    tr.addEventListener("click", (e) => { if (!e.target.closest("button")) toggle(); });
    return isOpen ? [tr, detailRow(a)] : [tr];
  }

  function detailRow(a) {
    const parts = [];
    const coverage = a.planned ? a.realtime / a.planned : 1;
    parts.push(el("p", { class: "bv-facts" },
      `Turer på områdets linjer med realtid: ${nf0.format(a.realtime)} av ${nf0.format(a.planned)} enligt tidtabell`,
      a.planned - a.realtime >= 3 && coverage < 0.85 ? el("strong", { class: "warn", text: ` – ${a.planned - a.realtime} saknar realtid (inställda eller utan data)` }) : null,
      a.max != null ? `. Största försening ${min1(a.max)}.` : "."));
    if (a.reasons.length) {
      parts.push(el("p", { class: "bv-facts" }, "Orsaker enligt larmen: " + a.reasons.map(([r, n]) => `${r} (${n})`).join(", ") + "."));
    }
    if (a.lines.length) {
      parts.push(el("table", { class: "bv-lines" },
        el("thead", {}, el("tr", {}, ...["Linje", "Bussar", "Sena 5+", "Snitt", "Max", "Larm", "Inställda"].map((h, i) => el("th", { class: i ? "t" : "", text: h })))),
        el("tbody", {}, ...a.lines.map((l) => el("tr", {},
          el("td", {}, el("strong", { text: l.line }), l.kind ? el("span", { class: "sub", text: l.kind }) : null),
          el("td", { class: "t", text: `${l.buses} (${l.planned})`, title: "i trafik med realtid (enligt tidtabell)" }),
          el("td", { class: "t", text: l.late5 || "–" }),
          el("td", { class: "t", text: min1(l.avg) }),
          el("td", { class: "t", text: min1(l.max) }),
          el("td", { class: "t", text: l.alerts || "–" }),
          el("td", { class: "t", text: l.cancelled || "–" }))))));
    }
    if (a.trip_alerts.length) {
      parts.push(el("div", { class: "bv-sub" }, el("h4", { text: "Larm om enskilda turer" }),
        el("ul", {}, ...a.trip_alerts.slice(0, 8).map((t) => el("li", {}, el("strong", { text: t.line + " " }), t.text)))));
    }
    if (a.new_alerts.length) {
      parts.push(el("div", { class: "bv-sub" }, el("h4", { text: "Nya meddelanden (senaste 3 h)" }),
        el("ul", {}, ...a.new_alerts.map((x) => el("li", {},
          el("strong", { text: x.title }), x.lines.length ? ` · linje ${x.lines.join(", ")}` : "", x.since ? ` · ${time(x.since)}` : "",
          x.text ? el("span", { class: "sub", text: x.text.slice(0, 280) }) : null)))));
    }
    if (a.planned_alerts) {
      parts.push(el("details", { class: "bv-planned" }, el("summary", { text: `Planerade ändringar (${a.planned_alerts})` }),
        el("ul", {}, ...a.planned_alert_list.map((x) => el("li", { text: x.title + (x.lines.length ? ` · ${x.lines.join(", ")}` : "") })))));
    }
    return el("tr", { class: "bv-detail" }, el("td", { colspan: "8" }, ...parts));
  }

  function areaList() {
    const issues = data.areas.filter((a) => a.status !== "normal");
    const normal = data.areas.filter((a) => a.status === "normal").sort((a, b) => b.buses - a.buses);
    const more = el("button", { type: "button", class: "bv-more", "aria-expanded": String(showNormal),
      text: showNormal ? "Dölj områden med normalt läge" : `Visa ${normal.length} områden med normalt läge` });
    more.addEventListener("click", () => { showNormal = !showNormal; render(); });
    const rows = [...issues.flatMap(areaRow)];
    if (!issues.length) rows.push(el("tr", {}, el("td", { colspan: "8", class: "bv-calm", text: "Inga områden har förseningar utöver det normala just nu." })));
    if (showNormal) rows.push(...normal.flatMap(areaRow));
    return el("div", { class: "bv-areas" },
      el("table", {},
        el("thead", {}, el("tr", {},
          el("th", { text: "Läge" }), el("th", { text: "Område" }), el("th", { class: "t", text: "Bussar" }),
          el("th", { class: "t", text: "Sena 5+ min" }), el("th", { class: "t hide-sm", text: "Snitt" }),
          el("th", { class: "hide-sm", text: "Senaste timmen" }), el("th", { class: "t", text: "Larm" }), el("th", { class: "t", text: "Inställda" }))),
        el("tbody", {}, ...rows)),
      normal.length ? more : null);
  }

  function hotspots() {
    if (!data.hotspots.length) return el("div");
    return el("section", { class: "bv-hotspots", "aria-label": "Brännpunkter" },
      el("h2", { text: "Flest sena bussar kring" }),
      el("ul", {}, ...data.hotspots.map((h) => {
        const li = el("li", {},
          el("strong", { text: h.name }), el("span", { class: "muted", text: ` · ${h.area}` }),
          el("span", { class: "sub", text: `${h.late} sena av ${h.buses} bussar i närheten, snitt ${Math.round(h.avg)} min · linje ${h.lines.join(", ")}` }));
        li.addEventListener("click", () => focusArea(h.area));
        return li;
      })));
  }

  window.BusView = { start, stop, render };
})();
