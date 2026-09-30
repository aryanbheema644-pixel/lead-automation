const $ = (s) => document.querySelector(s);
let pollTimer = null;
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
async function readResponse(r) {
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { detail: text.slice(0, 300) || `HTTP ${r.status}` }; }
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// ── Router ────────────────────────────────────────────────────────────────
const PAGES = { dashboard: "Dashboard", pipeline: "Pipeline", leads: "Leads", review: "Needs review" };
function setPage(page) {
  if (!PAGES[page]) page = "dashboard";
  document.querySelectorAll(".page").forEach((s) => (s.hidden = s.dataset.page !== page));
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === page));
  $("#pageTitle").textContent = PAGES[page];
}
window.addEventListener("hashchange", () => setPage(location.hash.slice(1)));

// ── Upload ──────────────────────────────────────────────────────────────────
const fileInput = $("#fileInput"), filedrop = $("#filedrop");
filedrop.addEventListener("dragover", (e) => { e.preventDefault(); filedrop.classList.add("drag"); });
filedrop.addEventListener("dragleave", () => filedrop.classList.remove("drag"));
filedrop.addEventListener("drop", (e) => { e.preventDefault(); filedrop.classList.remove("drag"); if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]); });
fileInput.addEventListener("change", () => { if (fileInput.files.length) uploadFile(fileInput.files[0]); });
async function uploadFile(file) {
  const msg = $("#uploadMsg"); msg.className = "msg"; msg.textContent = `Uploading ${file.name}…`;
  const fd = new FormData(); fd.append("file", file);
  try { const d = await readResponse(await fetch("/api/upload", { method: "POST", body: fd }));
    msg.className = "msg ok"; msg.textContent = `Added ${d.inserted} leads. Click “Run pipeline”.`; refresh();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}

// ── Actions ─────────────────────────────────────────────────────────────────
$("#runBtn").addEventListener("click", async () => {
  const b = $("#runBtn"); b.disabled = true;
  try { const d = await readResponse(await fetch("/api/run", { method: "POST" }));
    $("#uploadMsg").className = "msg ok"; $("#uploadMsg").textContent = `Processing ${d.queued} queued leads…`; startPolling();
  } catch (e) { $("#uploadMsg").className = "msg err"; $("#uploadMsg").textContent = e.message; }
  finally { setTimeout(() => (b.disabled = false), 1500); }
});
$("#resetBtn").addEventListener("click", async () => { if (!confirm("Delete all leads and results?")) return; await fetch("/api/reset", { method: "POST" }); refresh(); });
$("#pullBtn").addEventListener("click", async () => {
  const msg = $("#uploadMsg"); msg.className = "msg"; msg.textContent = "Pulling leads from the source sheet…";
  try { const d = await readResponse(await fetch("/api/sheets/pull", { method: "POST" }));
    const bd = d.tabs ? " (" + Object.entries(d.tabs).map(([t, n]) => `${t}: ${n}`).join(", ") + ")" : "";
    msg.className = "msg ok"; msg.textContent = `Pulled ${d.inserted} leads${bd}. Click “Run pipeline”.`; refresh();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
});
$("#pushBtn").addEventListener("click", async () => {
  const msg = $("#uploadMsg"); msg.className = "msg"; msg.textContent = "Pushing refined leads to the destination sheet…";
  try { const d = await readResponse(await fetch("/api/sheets/push", { method: "POST" }));
    msg.className = "msg ok"; msg.textContent = d.pushed ? `Pushed ${d.pushed} leads to the destination sheet.` : (d.note || "Nothing new to push.");
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
});
$("#baselineBtn").addEventListener("click", async () => {
  if (!confirm("Mark ALL current source rows as already-seen? They'll be SKIPPED — only new leads get processed.")) return;
  const msg = $("#uploadMsg"); msg.className = "msg"; msg.textContent = "Baselining the source backlog…";
  try { const d = await readResponse(await fetch("/api/sheets/baseline", { method: "POST" }));
    msg.className = "msg ok"; msg.textContent = `Baselined ${d.baselined} rows. Only new leads will be processed now.`;
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
});

// ── Pipeline animation ────────────────────────────────────────────────────
const STAGE_NODE = { extracting: 2, querying: 3, searching: 4, matching: 5, "scraping profile": 6, screening: 7, done: 8, provided: 8, no_linkedin: 8 };
function updatePipeline(st) {
  const pipe = $("#pipeline"); if (!pipe) return;
  const running = st.running && st.current;
  pipe.classList.toggle("running", !!running);
  const nodes = document.querySelectorAll("#snake .node");
  let active = 0;
  if (running) { active = STAGE_NODE[st.current.stage] || 1; $("#pipelineNow").textContent = `${st.current.stage || "working"} · ${st.current.name || ""}`.trim(); }
  else { const s = st.stats || {}; const done = (s.accepted || 0) + (s.review || 0) + (s.rejected || 0); active = done > 0 ? 8 : 0; $("#pipelineNow").textContent = done > 0 ? `Idle · ${done} processed` : "Idle"; }
  nodes.forEach((n) => { const i = +n.dataset.i; n.classList.toggle("done", running ? i < active : i <= active); n.classList.toggle("active", running && i === active); });
  const frac = active <= 1 ? 0 : ((active - 1) / 7) * 100;
  $("#trackDone").setAttribute("stroke-dasharray", `${(!running && active === 8) ? 100 : frac} 100`);
}

// ── Rendering helpers ───────────────────────────────────────────────────────
function badge(status, stage) {
  const label = status === "processing" && stage ? stage : status;
  const spin = status === "processing" ? '<span class="spin">◠</span>' : "";
  return `<span class="badge ${status}">${spin}${esc(label)}</span>`;
}
const SOURCE_LABELS = { linkedin: "LinkedIn profile", "linkedin (provided)": "LinkedIn (provided)", scholar: "Google Scholar / ResearchGate", github: "GitHub", company: "Company page", imdb: "IMDb", social: "Social profile", news: "News / article", pdf: "PDF document", "other link": "Other link", personal_or_other: "Personal site / other" };
function prettyUrl(u) { u = String(u || "").replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/$/, ""); return u.length > 40 ? u.slice(0, 40) + "…" : u; }
function topMatch(lead) {
  const c = (lead.candidates || [])[0], ch = lead.chosen; const src = ch && ch.url ? ch : c;
  if (!src) return '<span class="sub">—</span>';
  const label = SOURCE_LABELS[src.source_type] || src.source_type || "—";
  return `<div class="tm-label">${esc(label)}</div><a class="tm-url" href="${src.url}" target="_blank" rel="noopener">${esc(prettyUrl(src.url))}</a>`;
}
function tierChip(s) { if (!s || !s.tier) return '<span class="sub">—</span>'; const t = esc(s.tier); return `<span class="tier ${t}">${t}</span>`; }
function rowActions(l) {
  let b = "";
  if (l.status === "review") b += `<button class="mini ok" data-accept="${l.id}">Accept</button><button class="mini no" data-reject="${l.id}">Reject</button>`;
  else if (l.status === "accepted") b += `<button class="mini no" data-reject="${l.id}">Reject</button>`;
  b += `<button class="link-btn" data-open="${l.id}">Details ▸</button>`;
  return `<div class="row-actions">${b}</div>`;
}
function wireRowButtons(root) {
  root.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); openDrawer(b.dataset.open); }));
  root.querySelectorAll("[data-accept]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); acceptTop(b.dataset.accept); }));
  root.querySelectorAll("[data-reject]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); rejectLead(b.dataset.reject); }));
}
async function acceptTop(id) { await fetch(`/api/leads/${id}/choose`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_index: 0 }) }); refresh(); }
async function rejectLead(id) { await fetch(`/api/leads/${id}/reject`, { method: "POST" }); refresh(); }

// ── Views ───────────────────────────────────────────────────────────────────
async function loadLeads() {
  const { leads } = await (await fetch("/api/leads?status=all")).json();
  const tbody = $("#leadRows");
  if (!leads.length) { tbody.innerHTML = `<tr><td colspan="8" class="empty">No leads yet — upload a CSV or pull from your sheet.</td></tr>`; return; }
  tbody.innerHTML = leads.map((l) => `
    <tr>
      <td class="sub">${l.id}</td>
      <td class="name-cell">${esc(l.name || "—")}<div class="sub">${esc(l.email || "")}</div></td>
      <td>${l.channel ? `<span class="chip">${esc(l.channel)}</span>` : '<span class="sub">—</span>'}</td>
      <td>${badge(l.status, l.stage)}</td>
      <td>${topMatch(l)}</td>
      <td>${tierChip(l.screening)}</td>
      <td>${l.owner ? `<span class="ae">${esc(l.owner)}</span>` : '<span class="sub">—</span>'}</td>
      <td>${rowActions(l)}</td>
    </tr>`).join("");
  wireRowButtons(tbody);
}
async function loadReview() {
  const { leads } = await (await fetch("/api/leads?status=review")).json();
  $("#reviewCount").textContent = leads.length;
  const wrap = $("#reviewCards");
  if (!leads.length) { wrap.innerHTML = `<div class="review-empty">Nothing needs review — you're all caught up. 🎉</div>`; return; }
  wrap.innerHTML = leads.map((l) => {
    const ch = l.chosen || (l.candidates || [])[0] || {};
    const label = SOURCE_LABELS[ch.source_type] || ch.source_type || "—";
    return `<div class="review-card">
      <div class="rc-row"><h4>${esc(l.name || "Lead #" + l.id)}</h4>${tierChip(l.screening)}</div>
      <div class="rc-meta">${esc(l.company || l.email || "")}${l.owner ? " · AE: " + esc(l.owner) : ""}</div>
      <div><div class="tm-label">${esc(label)}</div>${ch.url ? `<a class="rc-url" href="${ch.url}" target="_blank" rel="noopener">${esc(prettyUrl(ch.url))}</a>` : ""}</div>
      <div class="cand-reason">${esc((l.reasoning || "").slice(0, 180))}</div>
      <div class="rc-actions"><button class="mini ok" data-accept="${l.id}">Accept</button><button class="mini no" data-reject="${l.id}">Reject</button><button class="link-btn" data-open="${l.id}">Details ▸</button></div>
    </div>`;
  }).join("");
  wireRowButtons(wrap);
}
async function loadStats() {
  const st = await (await fetch("/api/status")).json();
  const { stats, running, providers } = st;
  const tiles = [
    ["total", "Total", stats.total || 0, `${stats.queued || 0} queued · ${stats.processing || 0} processing`],
    ["accepted", "Accepted", stats.accepted || 0, ""], ["review", "Review", stats.review || 0, ""], ["rejected", "Rejected", stats.rejected || 0, ""],
  ];
  $("#stats").innerHTML = tiles.map(([c, lbl, n, sub]) => `<div class="tile ${c}"><div class="num">${n}</div><div class="lbl">${lbl}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`).join("");
  const p = providers, prov = [["OpenRouter", p.openrouter], ["Tavily", p.tavily], ["Firecrawl", p.firecrawl], ["Apify", p.apify], ["Screening", p.screening], ["Sheets", p.sheets]];
  if (st.scheduler && st.scheduler.enabled) prov.push([`Auto ${Math.round((st.scheduler.interval || 900) / 60)}m`, true]);
  $("#providers").innerHTML = prov.map(([n, on]) => `<span class="pill ${on ? "on" : ""}"><span class="dot"></span>${n}</span>`).join("");
  $("#pullBtn").hidden = !(p.sheets && p.source_sheet);
  $("#pushBtn").hidden = !(p.sheets && p.dest_sheet);
  $("#baselineBtn").hidden = !(p.sheets && p.source_sheet);
  updatePipeline(st);
  if (!running && (stats.queued === 0 && stats.processing === 0)) stopPolling();
  return running;
}

// ── Charts ────────────────────────────────────────────────────────────────
const PALETTE = ["#5b5bd6", "#0d9488", "#d9880b", "#e11d48", "#2563eb", "#7c3aed", "#16a34a", "#64748b"];
const TIER_COLOR = { GO: "#16a34a", NURTURE: "#2563eb", REVIEW: "#d9880b", "NO-GO": "#dc2626" };
const _charts = {};
const _ready = () => typeof Chart !== "undefined";
function _draw(id, cfg) { if (!_ready()) return; const el = document.getElementById(id); if (!el) return; if (_charts[id]) { _charts[id].data = cfg.data; _charts[id].update(); } else { _charts[id] = new Chart(el, cfg); } }
const _axis = () => ({ responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: { color: "#737a8c", font: { size: 11 } } }, y: { beginAtZero: true, grid: { color: "#eef0f5" }, ticks: { color: "#737a8c", precision: 0, font: { size: 11 } } } } });
function _bar(labels, data, colors) { return { type: "bar", data: { labels, datasets: [{ data, backgroundColor: colors || labels.map((_, i) => PALETTE[i % PALETTE.length]), borderRadius: 6, maxBarThickness: 48 }] }, options: _axis() }; }
function _donut(labels, data, colors) { return { type: "doughnut", data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: "#fff", borderWidth: 3 }] }, options: { responsive: true, maintainAspectRatio: false, cutout: "62%", plugins: { legend: { position: "bottom", labels: { color: "#737a8c", boxWidth: 10, usePointStyle: true, font: { size: 11 } } } } } }; }
function _line(labels, data) { return { type: "line", data: { labels, datasets: [{ data, borderColor: "#5b5bd6", backgroundColor: "rgba(91,91,214,.10)", fill: true, tension: .35, pointRadius: 2, pointBackgroundColor: "#5b5bd6" }] }, options: _axis() }; }
async function loadAnalytics() {
  if (!_ready()) { setTimeout(loadAnalytics, 400); return; }
  let a; try { a = await (await fetch("/api/analytics")).json(); } catch { return; }
  const ch = a.channels || {}; _draw("chChannels", _bar(Object.keys(ch).length ? Object.keys(ch) : ["—"], Object.values(ch).length ? Object.values(ch) : [0]));
  const oc = a.statuses || {}; _draw("chOutcomes", _donut(["Accepted", "Review", "Rejected"], [oc.accepted || 0, oc.review || 0, oc.rejected || 0], ["#16a34a", "#d9880b", "#dc2626"]));
  const ti = a.tiers || {}; const tl = Object.keys(ti); _draw("chTiers", _bar(tl.length ? tl : ["—"], tl.length ? tl.map(k => ti[k]) : [0], tl.map(k => TIER_COLOR[k] || "#64748b")));
  const ow = a.owners || {}; const ol = Object.keys(ow); _draw("chOwners", _bar(ol.length ? ol : ["—"], ol.length ? ol.map(k => ow[k]) : [0]));
  const days = a.by_day || []; _draw("chTime", _line(days.map(d => d[0].slice(5)), days.map(d => d[1])));
}

// ── Drawer ──────────────────────────────────────────────────────────────────
async function openDrawer(id) {
  const lead = await (await fetch(`/api/leads/${id}`)).json();
  const ex = lead.extracted || {}, cands = lead.candidates || [], scr = lead.screening || {};
  const candHtml = cands.map((c, i) => {
    const sig = c.signals || {}, reviewable = lead.status === "review" || lead.status === "accepted";
    const st = (label, v) => `<span class="sig ${v ? "yes" : "no"}">${label} ${v ? "✓" : "✕"}</span>`;
    return `<div class="cand ${i === 0 ? "best" : ""}">
      <div class="cand-head"><span class="cand-type">${esc(c.source_type)} · <span class="src-tag">via ${esc(c.content_source || "—")}</span></span><span class="cand-score">${c.score == null ? "" : (+c.score).toFixed(2)}</span></div>
      <b>${esc(c.person?.name || c.title || "")}</b>
      <a class="cand-url" href="${c.url}" target="_blank" rel="noopener">${esc(c.url)}</a>
      <div class="signals">${st("name", sig.name)}${st("company", sig.company)}${st("school", sig.school)}${st("location", sig.location)}${st("role", sig.role)}</div>
      <div class="cand-reason">${esc(c.reasoning || "")}</div>
      ${reviewable ? `<div class="rc-actions"><button class="mini ok" data-choose="${i}">Select this match</button></div>` : ""}
    </div>`;
  }).join("") || '<p class="sub">No candidates.</p>';
  const hasScreen = scr.tier || scr.answer || scr.note || scr.error;
  const kv = (k, v) => v ? `<span class="k">${k}</span><span>${esc(v)}</span>` : "";
  const screeningHtml = hasScreen ? `<h3>Screening verdict (RAG fit-eval)</h3>
    ${scr.tier ? `<div class="sub" style="margin-bottom:8px"><span class="tier ${esc(scr.tier)}">${esc(scr.tier)}</span> ${esc(scr.confidence || "")}</div>` : ""}
    <div class="kv">${kv("Best path", scr.best_path)}${kv("Backup path", scr.backup_path)}${kv("Key strength", scr.key_strength)}${kv("Red flag", scr.red_flag)}${kv("Flip trigger", scr.flip_trigger)}${kv("Matched cases", scr.matched_cases)}${kv("Note", scr.note)}${kv("Error", scr.error)}</div>
    ${scr.answer ? `<div class="cand-reason" style="white-space:pre-wrap;margin-top:8px">${esc(scr.answer)}</div>` : ""}` : "";
  $("#drawerBody").innerHTML = `
    <h2>${esc(lead.name || "Lead #" + lead.id)}</h2>
    <div class="sub">${badge(lead.status, lead.stage)}${lead.owner ? ` · AE: <b>${esc(lead.owner)}</b>` : ""}${lead.channel ? ` · ${esc(lead.channel)}` : ""}</div>
    ${screeningHtml}
    <h3>Raw input</h3>
    <div class="kv">${kv("Company", lead.company)}${kv("Email", lead.email)}${kv("Phone", lead.phone)}${kv("LinkedIn", lead.linkedin)}${kv("Visa", lead.visa)}${kv("Message", lead.message)}</div>
    ${ex.location || ex.role_guess || ex.school || ex.notes ? `<h3>Extracted &amp; enriched</h3><div class="kv">${kv("Company", ex.company)}${kv("School", ex.school)}${kv("Location", ex.location)}${kv("Role guess", ex.role_guess)}${kv("Notes", ex.notes)}</div>${(ex.keywords || []).length ? `<div class="tags" style="margin-top:8px">${ex.keywords.map(k => `<span class="tag">${esc(k)}</span>`).join("")}</div>` : ""}` : ""}
    ${lead.queries ? `<h3>Search queries</h3><div class="tags">${lead.queries.map(q => `<span class="tag">${esc(q)}</span>`).join("")}</div>` : ""}
    ${lead.error ? `<h3>Error</h3><div class="cand-reason" style="color:var(--red)">${esc(lead.error)}</div>` : ""}
    <h3>Candidates (${cands.length})</h3>${candHtml}
    ${lead.status === "review" || lead.status === "accepted" ? `<div style="margin-top:14px"><button class="mini no" id="rejectBtn">Reject lead</button></div>` : ""}`;
  $("#drawerBody").querySelectorAll("[data-choose]").forEach((b) => b.addEventListener("click", async () => { await fetch(`/api/leads/${id}/choose`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_index: +b.dataset.choose }) }); closeDrawer(); refresh(); }));
  const rej = $("#rejectBtn"); if (rej) rej.addEventListener("click", async () => { await fetch(`/api/leads/${id}/reject`, { method: "POST" }); closeDrawer(); refresh(); });
  $("#drawer").classList.add("open");
}
function closeDrawer() { $("#drawer").classList.remove("open"); }
$("#drawerClose").addEventListener("click", closeDrawer);
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeDrawer(); });

// ── Polling + boot ────────────────────────────────────────────────────────
function startPolling() { if (!pollTimer) pollTimer = setInterval(refresh, 2000); }
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }
function refresh() { loadStats(); loadLeads(); loadReview(); loadAnalytics(); }

setPage(location.hash.slice(1) || "dashboard");
refresh();
loadStats().then((running) => { if (running) startPolling(); });
