const $ = (s) => document.querySelector(s);
let pollTimer = null;
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
// Only touch the DOM when the markup actually changed — the dashboard polls every
// 2s while running, and blind re-renders make the page flicker and jump.
function setHTML(el, html) { if (!el || el._html === html) return false; el._html = html; el.innerHTML = html; return true; }
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
async function readResponse(r) {
  const t = await r.text(); let d; try { d = JSON.parse(t); } catch { d = { detail: t.slice(0, 300) || `HTTP ${r.status}` }; }
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`); return d;
}

// ── Theme ─────────────────────────────────────────────────────────────────
function setTheme(t) { document.documentElement.dataset.theme = t; try { localStorage.setItem("ls-theme", t); } catch {} $("#themeToggle").textContent = t === "dark" ? "☀" : "☾"; }
(function initTheme() { let t = "dark"; try { t = localStorage.getItem("ls-theme") || "dark"; } catch {} document.documentElement.dataset.theme = t; })();
$("#themeToggle").addEventListener("click", () => {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  Object.values(_charts).forEach((c) => c.destroy()); for (const k in _charts) delete _charts[k];
  loadAnalytics();
});

// ── Router ──────────────────────────────────────────────────────────────────
const PAGES = { dashboard: "Dashboard", pipeline: "Pipeline", leads: "Leads", review: "Needs review" };
function setPage(p) { if (!PAGES[p]) p = "dashboard"; document.querySelectorAll(".page").forEach((s) => (s.hidden = s.dataset.page !== p)); document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === p)); }
window.addEventListener("hashchange", () => setPage(location.hash.slice(1)));

// ── Greeting ────────────────────────────────────────────────────────────────
(function greet() { const h = new Date().getHours(); const g = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening"; const el = $("#greeting"); if (el) el.textContent = `Good ${g},`; })();

// ── Upload / actions ──────────────────────────────────────────────────────
const fileInput = $("#fileInput"), filedrop = $("#filedrop");
filedrop.addEventListener("dragover", (e) => { e.preventDefault(); filedrop.classList.add("drag"); });
filedrop.addEventListener("dragleave", () => filedrop.classList.remove("drag"));
filedrop.addEventListener("drop", (e) => { e.preventDefault(); filedrop.classList.remove("drag"); if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]); });
fileInput.addEventListener("change", () => { if (fileInput.files.length) uploadFile(fileInput.files[0]); });
async function uploadFile(file) {
  const m = $("#uploadMsg"); m.className = "msg"; m.textContent = `Uploading ${file.name}…`;
  const fd = new FormData(); fd.append("file", file);
  try { const d = await readResponse(await fetch("/api/upload", { method: "POST", body: fd })); m.className = "msg ok"; m.textContent = `Added ${d.inserted} leads. Click “Run pipeline”.`; refresh(); }
  catch (e) { m.className = "msg err"; m.textContent = e.message; }
}
$("#runBtn").addEventListener("click", async () => { const b = $("#runBtn"); b.disabled = true; try { const d = await readResponse(await fetch("/api/run", { method: "POST" })); $("#uploadMsg").className = "msg ok"; $("#uploadMsg").textContent = `Processing ${d.queued} queued leads…`; startPolling(); } catch (e) { $("#uploadMsg").className = "msg err"; $("#uploadMsg").textContent = e.message; } finally { setTimeout(() => (b.disabled = false), 1500); } });
$("#resetBtn").addEventListener("click", async () => { if (!confirm("Delete all leads and results?")) return; await fetch("/api/reset", { method: "POST" }); refresh(); });
$("#pullBtn").addEventListener("click", async () => { const m = $("#uploadMsg"); m.className = "msg"; m.textContent = "Pulling leads…"; try { const d = await readResponse(await fetch("/api/sheets/pull", { method: "POST" })); const bd = d.tabs ? " (" + Object.entries(d.tabs).map(([t, n]) => `${t}: ${n}`).join(", ") + ")" : ""; m.className = "msg ok"; m.textContent = `Pulled ${d.inserted} leads${bd}.`; refresh(); } catch (e) { m.className = "msg err"; m.textContent = e.message; } });
$("#pushBtn").addEventListener("click", async () => { const m = $("#uploadMsg"); m.className = "msg"; m.textContent = "Pushing…"; try { const d = await readResponse(await fetch("/api/sheets/push", { method: "POST" })); m.className = "msg ok"; m.textContent = d.pushed ? `Pushed ${d.pushed} leads.` : (d.note || "Nothing to push."); } catch (e) { m.className = "msg err"; m.textContent = e.message; } });
$("#baselineBtn").addEventListener("click", async () => { if (!confirm("Mark ALL current source rows as seen (skip them)?")) return; const m = $("#uploadMsg"); m.className = "msg"; m.textContent = "Baselining…"; try { const d = await readResponse(await fetch("/api/sheets/baseline", { method: "POST" })); m.className = "msg ok"; m.textContent = `Baselined ${d.baselined} rows.`; } catch (e) { m.className = "msg err"; m.textContent = e.message; } });

// ── Pipeline animation ──────────────────────────────────────────────────────
const STAGE_NODE = { extracting: 2, querying: 3, searching: 4, matching: 5, "scraping profile": 6, screening: 7, done: 8, provided: 8, no_linkedin: 8 };
function updatePipeline(st) {
  const pipe = $("#pipeline"); if (!pipe) return;
  const running = st.running && st.current; pipe.classList.toggle("running", !!running);
  let active = 0;
  if (running) { active = STAGE_NODE[st.current.stage] || 1; $("#pipelineNow").textContent = `${st.current.stage || "working"} · ${st.current.name || ""}`.trim(); }
  else { const s = st.stats || {}; const done = (s.accepted || 0) + (s.review || 0) + (s.rejected || 0); active = done > 0 ? 8 : 0; $("#pipelineNow").textContent = done > 0 ? `Idle · ${done} processed` : "Idle"; }
  document.querySelectorAll("#snake .node").forEach((n) => { const i = +n.dataset.i; n.classList.toggle("done", running ? i < active : i <= active); n.classList.toggle("active", running && i === active); });
  const frac = active <= 1 ? 0 : ((active - 1) / 7) * 100;
  $("#trackDone").setAttribute("stroke-dasharray", `${(!running && active === 8) ? 100 : frac} 100`);
}

// ── Render helpers ──────────────────────────────────────────────────────────
function badge(status, stage) { const label = status === "processing" && stage ? stage : status; const spin = status === "processing" ? '<span class="spin">◠</span>' : ""; return `<span class="badge ${status}">${spin}${esc(label)}</span>`; }
const SOURCE_LABELS = { linkedin: "LinkedIn profile", "linkedin (provided)": "LinkedIn (provided)", scholar: "Google Scholar / ResearchGate", github: "GitHub", company: "Company page", imdb: "IMDb", social: "Social profile", news: "News / article", pdf: "PDF document", "other link": "Other link", personal_or_other: "Personal site / other" };
function prettyUrl(u) { u = String(u || "").replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/$/, ""); return u.length > 40 ? u.slice(0, 40) + "…" : u; }
function topMatch(lead) { const c = (lead.candidates || [])[0], ch = lead.chosen; const src = ch && ch.url ? ch : c; if (!src) return '<span class="sub">—</span>'; const label = SOURCE_LABELS[src.source_type] || src.source_type || "—"; return `<div class="tm-label">${esc(label)}</div><a class="tm-url" href="${src.url}" target="_blank" rel="noopener">${esc(prettyUrl(src.url))}</a>`; }
function tierChip(s) { if (!s || !s.tier) return '<span class="sub">—</span>'; const t = esc(s.tier); return `<span class="tier ${t}">${t}</span>`; }
function rowActions(l) { let b = ""; if (l.status === "review") b += `<button class="mini ok" data-accept="${l.id}">Accept</button><button class="mini no" data-reject="${l.id}">Reject</button>`; else if (l.status === "accepted") b += `<button class="mini no" data-reject="${l.id}">Reject</button>`; b += `<button class="link-btn" data-open="${l.id}">Details ▸</button><button class="icon-del" data-delete="${l.id}" title="Delete lead" aria-label="Delete lead"><svg width="15" height="15" viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button>`; return `<div class="row-actions">${b}</div>`; }
function wireRowButtons(root) {
  root.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); openDrawer(b.dataset.open); }));
  root.querySelectorAll("[data-accept]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); acceptTop(b.dataset.accept); }));
  root.querySelectorAll("[data-reject]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); rejectLead(b.dataset.reject); }));
  root.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); deleteLead(b.dataset.delete); }));
}
async function deleteLead(id) {
  if (!confirm(`Delete lead #${id}? This can't be undone.`)) return false;
  try { await readResponse(await fetch(`/api/leads/${id}`, { method: "DELETE" })); } catch (e) { alert(e.message); return false; }
  refresh(); return true;
}
async function acceptTop(id) { await fetch(`/api/leads/${id}/choose`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_index: 0 }) }); refresh(); }
async function rejectLead(id) { await fetch(`/api/leads/${id}/reject`, { method: "POST" }); refresh(); }

// ── Views ───────────────────────────────────────────────────────────────────
async function loadLeads() {
  const { leads } = await (await fetch("/api/leads?status=all")).json();
  const tb = $("#leadRows");
  if (!leads.length) { setHTML(tb, `<tr><td colspan="8" class="empty">No leads yet — upload a CSV or pull from your sheet.</td></tr>`); return; }
  if (setHTML(tb, leads.map((l) => `<tr>
    <td class="sub">${l.id}</td>
    <td class="name-cell">${esc(l.name || "—")}<div class="sub">${esc(l.email || "")}</div></td>
    <td>${l.channel ? `<span class="chip">${esc(l.channel)}</span>` : '<span class="sub">—</span>'}</td>
    <td>${badge(l.status, l.stage)}</td>
    <td>${topMatch(l)}</td>
    <td>${tierChip(l.screening)}</td>
    <td>${l.owner ? `<span class="ae">${esc(l.owner)}</span>` : '<span class="sub">—</span>'}</td>
    <td>${rowActions(l)}</td></tr>`).join(""))) wireRowButtons(tb);
}
async function loadReview() {
  const { leads } = await (await fetch("/api/leads?status=review")).json();
  $("#reviewCount").textContent = leads.length;
  const w = $("#reviewCards");
  if (!leads.length) { setHTML(w, `<div class="review-empty">Nothing needs review — you're all caught up. 🎉</div>`); return; }
  if (setHTML(w, leads.map((l) => { const ch = l.chosen || (l.candidates || [])[0] || {}; const label = SOURCE_LABELS[ch.source_type] || ch.source_type || "—"; return `<div class="review-card">
    <div class="rc-row"><h4>${esc(l.name || "Lead #" + l.id)}</h4>${tierChip(l.screening)}</div>
    <div class="rc-meta">${esc(l.company || l.email || "")}${l.owner ? " · AE: " + esc(l.owner) : ""}</div>
    <div><div class="tm-label">${esc(label)}</div>${ch.url ? `<a class="rc-url" href="${ch.url}" target="_blank" rel="noopener">${esc(prettyUrl(ch.url))}</a>` : ""}</div>
    <div class="cand-reason">${esc((l.reasoning || "").slice(0, 180))}</div>
    <div class="rc-actions"><button class="mini ok" data-accept="${l.id}">Accept</button><button class="mini no" data-reject="${l.id}">Reject</button><button class="link-btn" data-open="${l.id}">Details ▸</button></div></div>`; }).join(""))) wireRowButtons(w);
}

const _IC = 'width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
const ICONS = {
  people: `<svg ${_IC}><circle cx="9" cy="8" r="3"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><path d="M16 6a3 3 0 0 1 0 6"/></svg>`,
  check: `<svg ${_IC}><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-5"/></svg>`,
  clock: `<svg ${_IC}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
  x: `<svg ${_IC}><circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/></svg>`,
};
function sparkline(vals) {
  if (!vals || vals.length < 2) vals = [1, 1];
  const w = 84, h = 30, max = Math.max(...vals), min = Math.min(...vals), rng = (max - min) || 1;
  const pts = vals.map((v, i) => [(i / (vals.length - 1)) * w, h - 2 - ((v - min) / rng) * (h - 5)]);
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d} L ${w} ${h} L 0 ${h} Z" fill="var(--muted)" opacity=".12"/><path d="${d}" fill="none" stroke="var(--muted)" stroke-width="1.6"/></svg>`;
}
// Filled by loadAnalytics, rendered inside the tiles (so a tile re-render never
// leaves an empty slot that pops in later and shifts the layout).
let _spark = sparkline([1, 1]), _trend = { text: "", cls: "trend" };
async function loadStats() {
  const st = await (await fetch("/api/status")).json();
  const { stats, running, providers } = st;
  const tot = stats.total || 0, pct = (n) => (tot ? ((n / tot) * 100).toFixed(1) + "%" : "0%");
  const tiles = [
    { c: "total", ic: ICONS.people, title: "Total Leads", badge: `<span id="trendTotal" class="${_trend.cls}">${_trend.text}</span>`, num: tot, sub: `${stats.queued || 0} queued · ${stats.processing || 0} processing` },
    { c: "accepted", ic: ICONS.check, title: "Accepted", badge: `<span class="trend up">${pct(stats.accepted || 0)}</span>`, num: stats.accepted || 0, sub: "of total" },
    { c: "review", ic: ICONS.clock, title: "In Review", badge: `<span class="trend">${pct(stats.review || 0)}</span>`, num: stats.review || 0, sub: "of total" },
    { c: "rejected", ic: ICONS.x, title: "Rejected", badge: `<span class="trend down">${pct(stats.rejected || 0)}</span>`, num: stats.rejected || 0, sub: "of total" },
  ];
  setHTML($("#stats"), tiles.map((t) => `<div class="panel tile ${t.c}"><div class="ic">${t.ic}</div><div class="body">
    <div class="t-row"><span class="t-title">${t.title}</span>${t.badge}</div>
    <div class="t-num">${t.num}</div>
    <div class="t-row2"><span class="t-sub">${t.sub}</span><span class="spark-slot">${_spark}</span></div></div></div>`).join(""));
  const prov =[["OpenRouter", providers.openrouter], ["Tavily", providers.tavily], ["Firecrawl", providers.firecrawl], ["Apify", providers.apify], ["Screening", providers.screening], ["Sheets", providers.sheets]];
  if (st.scheduler && st.scheduler.enabled) prov.push([`Auto ${Math.round((st.scheduler.interval || 900) / 60)}m`, true]);
  setHTML($("#providers"), prov.map(([n, on]) => `<li class="${on ? "on" : ""}"><span class="dot"></span>${n}</li>`).join(""));
  $("#pullBtn").hidden = !(providers.sheets && providers.source_sheet);
  $("#pushBtn").hidden = !(providers.sheets && providers.dest_sheet);
  $("#baselineBtn").hidden = !(providers.sheets && providers.source_sheet);
  updatePipeline(st);
  if (!running && (stats.queued === 0 && stats.processing === 0)) stopPolling();
  return running;
}

// ── Charts ────────────────────────────────────────────────────────────────
const _charts = {};
const _ready = () => typeof Chart !== "undefined";
function _grad(ctx, a, b) { const ch = ctx.chart, area = ch.chartArea; if (!area) return cssVar(a); const g = ch.ctx.createLinearGradient(0, area.top, 0, area.bottom); g.addColorStop(0, cssVar(a)); g.addColorStop(1, cssVar(b)); return g; }
// Animate on first draw only; afterwards update silently, and skip entirely when
// the data hasn't changed (polling would otherwise replay the animation every 2s).
function _draw(id, cfg) {
  if (!_ready()) return; const el = document.getElementById(id); if (!el) return;
  const sig = JSON.stringify([cfg.data.labels, cfg.data.datasets.map((d) => d.data)]);
  const ch = _charts[id];
  if (!ch) { _charts[id] = new Chart(el, cfg); _charts[id]._sig = sig; return; }
  if (ch._sig === sig) return;
  ch._sig = sig; ch.data = cfg.data; ch.options = cfg.options; ch.update("none");
}
function _axisOpts() { const grid = cssVar("--border"), tick = cssVar("--muted"); return { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { enabled: true } }, scales: { x: { grid: { display: false }, ticks: { color: tick, font: { size: 11 } } }, y: { beginAtZero: true, grid: { color: grid }, ticks: { color: tick, precision: 0, font: { size: 11 } } } } }; }
function _bar(labels, data) { return { type: "bar", data: { labels, datasets: [{ data, backgroundColor: (c) => _grad(c, "--bar-top", "--bar-bot"), borderRadius: 7, maxBarThickness: 46 }] }, options: _axisOpts() }; }
function _line(labels, data) { return { type: "line", data: { labels, datasets: [{ data, borderColor: cssVar("--accent"), backgroundColor: (c) => _grad(c, "--accent", "--bg"), fill: true, tension: .35, pointRadius: 2, pointBackgroundColor: cssVar("--accent") }] }, options: _axisOpts() }; }
function _donut(data, colors) { return { type: "doughnut", data: { labels: ["Accepted", "In Review", "Rejected"], datasets: [{ data, backgroundColor: colors, borderColor: cssVar("--panel"), borderWidth: 3 }] }, options: { responsive: true, maintainAspectRatio: false, cutout: "70%", plugins: { legend: { display: false } } } }; }
async function loadAnalytics() {
  if (!_ready()) { setTimeout(loadAnalytics, 400); return; }
  let a; try { a = await (await fetch("/api/analytics")).json(); } catch { return; }
  const ch = a.channels || {}; _draw("chChannels", _bar(Object.keys(ch).length ? Object.keys(ch) : ["—"], Object.values(ch).length ? Object.values(ch) : [0]));
  const ti = a.tiers || {}; const tl = Object.keys(ti); _draw("chTiers", _bar(tl.length ? tl : ["—"], tl.length ? tl.map((k) => ti[k]) : [0]));
  const ow = a.owners || {}; const ol = Object.keys(ow); _draw("chOwners", _bar(ol.length ? ol : ["—"], ol.length ? ol.map((k) => ow[k]) : [0]));
  const days = a.by_day || []; const series = days.map((d) => d[1]); _draw("chTime", _line(days.map((d) => d[0].slice(5)), series));
  // Outcomes donut + legend
  const oc = a.statuses || {}, acc = oc.accepted || 0, rev = oc.review || 0, rej = oc.rejected || 0, tot = acc + rev + rej;
  const sw = [cssVar("--accent"), cssVar("--accent-2"), cssVar("--faint")];
  _draw("chOutcomes", _donut([acc, rev, rej], sw));
  if ($("#ocTotal").textContent !== String(tot)) $("#ocTotal").textContent = tot;
  const pct = (n) => (tot ? (n / tot * 100).toFixed(1) : "0.0") + "%";
  const rows = [["Accepted", acc, sw[0]], ["In Review", rev, sw[1]], ["Rejected", rej, sw[2]]];
  setHTML($("#ocLegend"), rows.map(([lbl, n, color]) => `<li>
    <span class="lg"><span class="sw" style="background:${color}"></span>${lbl}</span>
    <span class="cnt">${n}</span><span class="pct">${pct(n)}</span>
    <span class="bar"><span style="width:${tot ? (n / tot * 100) : 0}%;background:${color}"></span></span></li>`).join(""));
  // sparklines + total trend: stored for loadStats, re-render tiles only if changed
  const sp = sparkline(series.length ? series : [tot, tot]);
  let trend = { text: "", cls: "trend" };
  if (series.length >= 2) { const half = Math.ceil(series.length / 2), older = series.slice(0, half).reduce((x, y) => x + y, 0), recent = series.slice(half).reduce((x, y) => x + y, 0); const pc = older ? Math.round((recent - older) / older * 100) : (recent ? 100 : 0); trend = { text: `${pc >= 0 ? "↗" : "↘"} ${Math.abs(pc)}%`, cls: "trend " + (pc >= 0 ? "up" : "down") }; }
  if (sp !== _spark || trend.text !== _trend.text) { _spark = sp; _trend = trend; loadStats(); }
}

// ── Drawer ──────────────────────────────────────────────────────────────────
async function openDrawer(id) {
  const lead = await (await fetch(`/api/leads/${id}`)).json();
  const ex = lead.extracted || {}, cands = lead.candidates || [], scr = lead.screening || {};
  const candHtml = cands.map((c, i) => { const sig = c.signals || {}, rv = lead.status === "review" || lead.status === "accepted"; const st = (l, v) => `<span class="sig ${v ? "yes" : "no"}">${l} ${v ? "✓" : "✕"}</span>`;
    return `<div class="cand ${i === 0 ? "best" : ""}"><div class="cand-head"><span class="cand-type">${esc(c.source_type)}${c.provided ? " · <b>provided by lead</b>" : ""} · <span class="src-tag">via ${esc(c.content_source || "—")}</span></span><span class="cand-score">${c.score == null ? "" : (+c.score).toFixed(2)}</span></div>
      <b>${esc(c.person?.name || c.title || "")}</b><a class="cand-url" href="${c.url}" target="_blank" rel="noopener">${esc(c.url)}</a>
      <div class="signals">${st("name", sig.name)}${st("company", sig.company)}${st("school", sig.school)}${st("location", sig.location)}${st("role", sig.role)}</div>
      <div class="cand-reason">${esc(c.reasoning || "")}</div>${rv ? `<div class="rc-actions"><button class="mini ok" data-choose="${i}">Select this match</button></div>` : ""}</div>`; }).join("") || '<p class="sub">No candidates.</p>';
  const kv = (k, v) => v ? `<span class="k">${k}</span><span>${esc(v)}</span>` : "";
  const hs = scr.tier || scr.answer || scr.note || scr.error;
  const screen = hs ? `<h3>Screening verdict (RAG fit-eval)</h3>${scr.tier ? `<div class="sub" style="margin-bottom:8px"><span class="tier ${esc(scr.tier)}">${esc(scr.tier)}</span> ${esc(scr.confidence || "")}</div>` : ""}<div class="kv">${kv("Best path", scr.best_path)}${kv("Backup path", scr.backup_path)}${kv("Key strength", scr.key_strength)}${kv("Red flag", scr.red_flag)}${kv("Flip trigger", scr.flip_trigger)}${kv("Matched cases", scr.matched_cases)}${kv("Note", scr.note)}${kv("Error", scr.error)}</div>${scr.answer ? `<div class="cand-reason" style="white-space:pre-wrap;margin-top:8px">${esc(scr.answer)}</div>` : ""}` : "";
  $("#drawerBody").innerHTML = `<h2>${esc(lead.name || "Lead #" + lead.id)}</h2>
    <div class="sub">${badge(lead.status, lead.stage)}${lead.owner ? ` · AE: <b>${esc(lead.owner)}</b>` : ""}${lead.channel ? ` · ${esc(lead.channel)}` : ""}</div>
    ${screen}<h3>Raw input</h3><div class="kv">${kv("Company", lead.company)}${kv("Email", lead.email)}${kv("Phone", lead.phone)}${kv("LinkedIn", lead.li_optout ? "Lead says they don't have one (LinkedIn search skipped, not screened)" : lead.linkedin)}${kv("Link given", lead.other_link)}${kv("Visa", lead.visa)}${kv("Message", lead.message)}</div>
    ${ex.location || ex.role_guess || ex.school || ex.notes ? `<h3>Extracted &amp; enriched</h3><div class="kv">${kv("Company", ex.company)}${kv("School", ex.school)}${kv("Location", ex.location)}${kv("Role guess", ex.role_guess)}${kv("Notes", ex.notes)}</div>${(ex.keywords || []).length ? `<div class="tags" style="margin-top:8px">${ex.keywords.map((k) => `<span class="tag">${esc(k)}</span>`).join("")}</div>` : ""}` : ""}
    ${lead.queries ? `<h3>Search queries</h3><div class="tags">${lead.queries.map((q) => `<span class="tag">${esc(q)}</span>`).join("")}</div>` : ""}
    ${lead.error ? `<h3>Error</h3><div class="cand-reason" style="color:var(--bad)">${esc(lead.error)}</div>` : ""}
    <h3>Candidates (${cands.length})</h3>${candHtml}
    <div class="row-actions" style="justify-content:flex-start;margin-top:14px">${lead.status === "review" || lead.status === "accepted" ? `<button class="mini no" id="rejectBtn">Reject lead</button>` : ""}<button class="mini no" id="deleteBtn">Delete lead</button></div>`;
  $("#drawerBody").querySelectorAll("[data-choose]").forEach((b) => b.addEventListener("click", async () => { await fetch(`/api/leads/${id}/choose`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_index: +b.dataset.choose }) }); closeDrawer(); refresh(); }));
  const rej = $("#rejectBtn"); if (rej) rej.addEventListener("click", async () => { await fetch(`/api/leads/${id}/reject`, { method: "POST" }); closeDrawer(); refresh(); });
  $("#deleteBtn").addEventListener("click", async () => { if (await deleteLead(id)) closeDrawer(); });
  $("#drawer").classList.add("open");
}
function closeDrawer() { $("#drawer").classList.remove("open"); }
$("#drawerClose").addEventListener("click", closeDrawer);
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeDrawer(); });

// ── Boot ────────────────────────────────────────────────────────────────────
function startPolling() { if (!pollTimer) pollTimer = setInterval(refresh, 2000); }
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }
function refresh() { loadStats(); loadLeads(); loadReview(); loadAnalytics(); }
setTheme(document.documentElement.dataset.theme || "dark");
setPage(location.hash.slice(1) || "dashboard");
refresh();
loadStats().then((r) => { if (r) startPolling(); });
