const $ = (s) => document.querySelector(s);
let pollTimer = null, _rescreening = false, _aes = [];
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
// Only touch the DOM when the markup actually changed — the dashboard polls every
// 2s while running, and blind re-renders make the page flicker and jump.
function setHTML(el, html) { if (!el || el._html === html) return false; el._html = html; el.innerHTML = html; return true; }
const _isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
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
const PAGES = { dashboard: "Dashboard", pipeline: "Pipeline", leads: "Leads", review: "Needs review", duplicates: "Duplicates", pipedrive: "Pipedrive", gtm: "GTM report", written: "Written on Pipedrive" };
// "#leads/rejected" opens the Leads page filtered to that status (dashboard tiles link here).
let _statusFilter = "all";
function setPage(hash) {
  let [p, sub] = String(hash || "").split("/");
  if (!PAGES[p]) p = "dashboard";
  if (p === "leads") { const s = sub || "all"; if (s !== _statusFilter) { _statusFilter = s; if (typeof loadLeads === "function") loadLeads(); } }
  document.querySelectorAll(".page").forEach((s) => (s.hidden = s.dataset.page !== p));
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === p));
}
window.addEventListener("hashchange", () => setPage(location.hash.slice(1)));

// ── Greeting ────────────────────────────────────────────────────────────────
(function greet() { const h = new Date().getHours(); const g = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening"; const el = $("#greeting"); if (el) el.textContent = `Good ${g},`; })();

// ── Upload / actions ──────────────────────────────────────────────────────
const fileInput = $("#fileInput"), filedrop = $("#filedrop");
filedrop.addEventListener("dragover", (e) => { e.preventDefault(); filedrop.classList.add("drag"); });
filedrop.addEventListener("dragleave", () => filedrop.classList.remove("drag"));
filedrop.addEventListener("drop", (e) => { e.preventDefault(); filedrop.classList.remove("drag"); if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]); });
fileInput.addEventListener("change", () => { if (fileInput.files.length) uploadFile(fileInput.files[0]); });
const _FD_IDLE = { icon: "⬆", text: "Drop a leads CSV or <b>browse</b>", hint: "or use ↓ Pull to read from Google Sheets" };
function setDrop(state, icon, text, hint) {
  filedrop.classList.remove("busy", "done", "fail"); if (state) filedrop.classList.add(state);
  $("#fdIcon").innerHTML = icon; $("#fdText").innerHTML = text; $("#fdHint").innerHTML = hint;
}
let _uploading = false, _dropReset = null;
async function uploadFile(file) {
  if (_uploading) return;
  _uploading = true; clearTimeout(_dropReset);
  // The drop box is on the Pipeline page, so it shows the progress itself.
  setDrop("busy", '<span class="fd-spinner"></span>', `Uploading <b>${esc(file.name)}</b> — reading the columns and adding leads…`, "This takes a few seconds. Please keep this page open.");
  const m = $("#uploadMsg"); m.className = "msg"; m.textContent = `Uploading ${file.name}…`;
  const fd = new FormData(); fd.append("file", file);
  try {
    const d = await readResponse(await fetch("/api/upload", { method: "POST", body: fd }));
    const blocks = (d.blocks || []).length > 1 ? ` · ${d.blocks.length} different column layouts detected and mapped separately` : "";
    setDrop("done", "✓", `Added <b>${d.inserted}</b> lead${d.inserted === 1 ? "" : "s"} from ${esc(file.name)}${blocks}`, `<a href="#leads" class="fd-link">View leads →</a> · then click ▶ Run pipeline`);
    m.className = "msg ok"; m.textContent = `Added ${d.inserted} leads${blocks}. Click “Run pipeline”.`;
    refresh();
  } catch (e) {
    setDrop("fail", "⚠", `Upload failed: ${esc(e.message)}`, "Fix the file and drop it again.");
    m.className = "msg err"; m.textContent = e.message;
  } finally {
    _uploading = false; fileInput.value = "";
    _dropReset = setTimeout(() => setDrop("", _FD_IDLE.icon, _FD_IDLE.text, _FD_IDLE.hint), 12000);
  }
}
$("#runBtn").addEventListener("click", async () => { const b = $("#runBtn"); b.disabled = true; try { const d = await readResponse(await fetch("/api/run", { method: "POST" })); $("#uploadMsg").className = "msg ok"; $("#uploadMsg").textContent = `Processing ${d.queued} queued leads…`; startPolling(); } catch (e) { $("#uploadMsg").className = "msg err"; $("#uploadMsg").textContent = e.message; } finally { setTimeout(() => (b.disabled = false), 1500); } });
$("#resetBtn").addEventListener("click", async () => { if (!confirm("Delete all leads and results?")) return; await fetch("/api/reset", { method: "POST" }); refresh(); });
$("#pullBtn").addEventListener("click", async () => { const m = $("#uploadMsg"); m.className = "msg"; m.textContent = "Pulling leads…"; try { const d = await readResponse(await fetch("/api/sheets/pull", { method: "POST" })); const bd = d.tabs ? " (" + Object.entries(d.tabs).map(([t, n]) => `${t}: ${n}`).join(", ") + ")" : ""; m.className = "msg ok"; m.textContent = `Pulled ${d.inserted} leads${bd}.`; refresh(); } catch (e) { m.className = "msg err"; m.textContent = e.message; } });
$("#pushBtn").addEventListener("click", async () => {
  const m = $("#uploadMsg");
  if (_autoPushing) { m.className = "msg"; m.textContent = "Automation is already pushing leads to the sheet automatically — no need to click Push. Pause “Push” in the automation bar to push manually."; return; }
  m.className = "msg"; m.textContent = "Pushing…";
  try {
    const d = await readResponse(await fetch("/api/sheets/push", { method: "POST" }));
    m.className = "msg ok"; const parts = []; if (d.added) parts.push(`added ${d.added}`); if (d.updated) parts.push(`updated ${d.updated}`);
    m.textContent = (parts.length ? `Sheet synced — ${parts.join(", ")}` : "Sheet already up to date") + ` · ${d.unchanged || 0} unchanged` + (d.in_review ? ` · ${d.in_review} in review not pushed (accept or reject them first)` : "") + "." + ((d.incomplete || []).length ? ` Held back, incomplete: ${d.incomplete.join("; ")}.` : "") + (d.report && d.report.changed ? ` GTM report: ${d.report.changed} cell(s) recounted.` : "") + (d.marked_in_progress ? ` ${d.marked_in_progress} lead(s) marked In Progress in the lead sheet.` : "") + (d.source_issue ? ` ${d.source_issue}` : "") + (d.report_issue ? ` GTM report issue: ${d.report_issue}` : "");
  } catch (e) { m.className = "msg err"; m.textContent = e.message; }
});
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
// AE column: a dropdown right in the table (merged-away duplicates just show text).
let _ownerById = {};
function aeCell(l) {
  // A merged-away duplicate shows (and changes) the merged lead's AE: same person, one AE.
  const owner = l.status === "duplicate" && l.merged_into ? (_ownerById[l.merged_into] || "") : l.owner;
  if (!_aes.length) return owner ? `<span class="ae">${esc(owner)}</span>` : '<span class="sub">—</span>';
  const title = l.status === "duplicate" ? `Same person as #${l.merged_into} — sets #${l.merged_into}'s AE` : "Change AE — updates the Pipedrive sheet and GTM report";
  return `<select class="ae-select" data-ae="${l.id}" title="${title}">${owner ? "" : '<option value="" selected>—</option>'}${_aes.map((a) => `<option ${a === owner ? "selected" : ""}>${esc(a)}</option>`).join("")}</select>`;
}
async function changeAE(id, owner, sel) {
  sel.disabled = true;
  try {
    const d = await readResponse(await fetch(`/api/leads/${id}/owner`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ owner }) }));
    sel.classList.add("saved"); sel.title = d.synced ? "Saved — updating the Pipedrive sheet and GTM report now" : "Saved";
    setTimeout(refresh, 600);
  } catch (e) { alert(e.message); refresh(); }
  finally { sel.disabled = false; }
}
function wireRowButtons(root) {
  root.querySelectorAll("[data-ae]").forEach((s) => { s.addEventListener("click", (e) => e.stopPropagation()); s.addEventListener("change", (e) => { e.stopPropagation(); if (s.value) changeAE(s.dataset.ae, s.value, s); }); });
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
// Accept keeps the lead's own chosen profile (or none, when nothing was found
// online) — picking a different candidate is done in Details.
async function acceptTop(id) { try { await readResponse(await fetch(`/api/leads/${id}/accept`, { method: "POST" })); } catch (e) { alert(e.message); } refresh(); }
async function rejectLead(id) { await fetch(`/api/leads/${id}/reject`, { method: "POST" }); refresh(); }

// ── Views ───────────────────────────────────────────────────────────────────
// ── Leads: filter by channel ─────────────────────────────────────────────
const _CHAN_LABEL = { all: "All", Meta: "Meta", Website: "Website", WhatsApp: "WhatsApp (bot)" };
const _CHAN_ORDER = ["Meta", "Website", "WhatsApp"];
let _chanFilter = "all";
try { _chanFilter = localStorage.getItem("ls-chan") || "all"; } catch {}
function renderChannelFilter(leads) {
  const counts = {};
  leads.forEach((l) => { const c = l.channel || "Not provided"; counts[c] = (counts[c] || 0) + 1; });
  // Meta / Website / WhatsApp always shown; any other channel only if it has leads.
  const chans = [..._CHAN_ORDER, ...Object.keys(counts).filter((c) => !_CHAN_ORDER.includes(c)).sort()];
  if (_chanFilter !== "all" && !chans.includes(_chanFilter)) _chanFilter = "all";
  const chip = (key, label, n) => `<button class="chan-chip${_chanFilter === key ? " active" : ""}" data-chan="${esc(key)}" role="tab" aria-selected="${_chanFilter === key}">${esc(label)} <span class="n">${n}</span></button>`;
  setHTML($("#chanFilter"), chip("all", "All", leads.length) + chans.map((c) => chip(c, _CHAN_LABEL[c] || c, counts[c] || 0)).join(""));
}
$("#chanFilter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-chan]"); if (!b) return;
  _chanFilter = b.dataset.chan;
  try { localStorage.setItem("ls-chan", _chanFilter); } catch {}
  loadLeads();
});

function renderStatusBar() {
  const el = $("#statusBar"); if (!el) return;
  el.hidden = _statusFilter === "all";
  setHTML(el, _statusFilter === "all" ? "" : `Showing: <b>${esc(_statusFilter)}</b> leads <a href="#leads" class="sb-clear" title="Show all statuses">✕ show all</a>`);
}

async function loadLeads() {
  const { leads } = await (await fetch("/api/leads?status=all")).json();
  // A reviewer-triggered re-screen runs outside the queue; keep polling until it lands.
  _rescreening = leads.some((l) => l.screening && l.screening.note === "Screening this LinkedIn profile…");
  if (_rescreening) startPolling();
  const tb = $("#leadRows");
  if (!leads.length) { setHTML(tb, `<tr><td colspan="8" class="empty">No leads yet — upload a CSV or pull from your sheet.</td></tr>`); return; }
  _ownerById = Object.fromEntries(leads.map((x) => [x.id, x.owner]));
  const _dupTargets = new Set(leads.filter((x) => x.dup_of && x.status !== "duplicate").map((x) => x.dup_of));
  renderChannelFilter(leads);
  renderStatusBar();
  const shown = leads.filter((l) => (_chanFilter === "all" || (l.channel || "Not provided") === _chanFilter)
                                 && (_statusFilter === "all" || l.status === _statusFilter));
  if (!shown.length) { setHTML(tb, `<tr><td colspan="8" class="empty">No ${_statusFilter === "all" ? "" : esc(_statusFilter) + " "}${_chanFilter === "all" ? "" : esc(_CHAN_LABEL[_chanFilter] || _chanFilter) + " "}leads.</td></tr>`); return; }
  if (setHTML(tb, shown.map((l) => `<tr>
    <td class="sub">${l.id}</td>
    <td class="name-cell">${esc(l.name || "—")}<div class="sub">${esc(l.email || "")}</div></td>
    <td>${l.channel ? `<span class="chip">${esc(l.channel)}</span>` : '<span class="sub">—</span>'}</td>
    <td>${l.status === "duplicate" ? (l.prior_status && l.prior_status !== "queued" ? badge(l.prior_status) : '<span class="badge duplicate">merged on arrival</span>') : badge(l.status, l.stage)}${l.dup_of && l.status !== "duplicate" ? `<div><span class="dup-flag" title="${esc(l.dup_reason || "")}">possible duplicate of #${l.dup_of}</span></div>` : _dupTargets.has(l.id) ? `<div><span class="dup-flag">possible duplicate — check Duplicates</span></div>` : ""}${l.status === "duplicate" && l.merged_into ? `<div class="sub dup-merged">duplicate · merged into #${l.merged_into}</div>` : ""}</td>
    <td>${topMatch(l)}</td>
    <td>${tierChip(l.screening)}</td>
    <td>${aeCell(l)}</td>
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

function _dupSide(l, label) {
  const ch = l.chosen || {}; const li = (ch.url && ch.url.includes("linkedin")) ? ch.url : (l.linkedin || "");
  const row = (k, v) => v ? `<div class="dk">${k}</div><div class="dv">${esc(v)}</div>` : "";
  return `<div class="dup-side"><div class="dup-label">${label} · #${l.id}</div><h4>${esc(l.name || "—")}</h4>
    <div class="dup-grid">${row("Channel", l.channel)}${row("Email", l.email)}${row("Phone", l.phone)}${row("LinkedIn", li ? prettyUrl(li) : "")}${row("Visa", l.visa)}${row("Date", l.lead_date)}${row("Status", l.status)}${row("AE", l.owner)}${row("Message", (l.message || "").slice(0, 220))}</div></div>`;
}
async function loadDuplicates() {
  let pairs = []; try { pairs = (await (await fetch("/api/duplicates")).json()).pairs || []; } catch { return; }
  $("#dupCount").textContent = pairs.length;
  const w = $("#dupCards");
  if (!pairs.length) { setHTML(w, `<div class="review-empty">No possible duplicates to check. 🎉</div>`); return; }
  if (setHTML(w, pairs.map((p) => `<div class="dup-card">
      <div class="dup-head"><span class="dup-flag">${esc(p.reason || "possible duplicate")}</span><span class="sub">Are these the same person?</span></div>
      <div class="dup-pair">${_dupSide(p.other, "Existing")}${_dupSide(p.lead, "New")}</div>
      <div class="rc-actions"><button class="mini ok" data-same="${p.lead.id}">Same person — merge</button><button class="mini" data-diff="${p.lead.id}">Different people — keep both</button></div>
    </div>`).join(""))) {
    w.querySelectorAll("[data-same],[data-diff]").forEach((b) => b.addEventListener("click", async () => {
      const id = b.dataset.same || b.dataset.diff; b.disabled = true;
      try { await readResponse(await fetch(`/api/duplicates/${id}/resolve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ same: !!b.dataset.same }) })); refresh(); }
      catch (e) { alert(e.message); b.disabled = false; }
    }));
  }
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
  if (st.aes) { const first = !_aes.length; _aes = st.aes; if (first) loadLeads(); }
  renderTeam(st.aes || [], st.aes_on_leave || []);
  renderSheetLinks(st.sheet_links || []);
  $("#dupCount").textContent = st.duplicate_checks || 0;
  const tot = stats.total || 0, pct = (n) => (tot ? ((n / tot) * 100).toFixed(1) + "%" : "0%");
  const tiles = [
    { c: "total", ic: ICONS.people, title: "Total Leads", badge: `<span id="trendTotal" class="${_trend.cls}">${_trend.text}</span>`, num: tot, sub: `${stats.queued || 0} queued · ${stats.processing || 0} processing` },
    { c: "accepted", ic: ICONS.check, title: "Accepted", badge: `<span class="trend up">${pct(stats.accepted || 0)}</span>`, num: stats.accepted || 0, sub: "of total" },
    { c: "review", ic: ICONS.clock, title: "In Review", badge: `<span class="trend">${pct(stats.review || 0)}</span>`, num: stats.review || 0, sub: "of total" },
    { c: "rejected", ic: ICONS.x, title: "Rejected", badge: `<span class="trend down">${pct(stats.rejected || 0)}</span>`, num: stats.rejected || 0, sub: "of total" },
  ];
  setHTML($("#stats"), tiles.map((t) => `<a class="panel tile ${t.c} tile-link" href="#leads/${t.c === "total" ? "all" : t.c}" title="Show ${t.c === "total" ? "all" : t.c} leads"><div class="ic">${t.ic}</div><div class="body">
    <div class="t-row"><span class="t-title">${t.title}</span>${t.badge}</div>
    <div class="t-num">${t.num}</div>
    <div class="t-row2"><span class="t-sub">${t.sub}</span><span class="spark-slot">${_spark}</span></div></div></a>`).join(""));
  const prov =[["OpenRouter", providers.openrouter], ["Tavily", providers.tavily], ["Firecrawl", providers.firecrawl], ["Apify", providers.apify], ["Screening", providers.screening], ["Sheets", providers.sheets]];
  if (st.scheduler && st.scheduler.enabled) prov.push([`Auto ${Math.round((st.scheduler.interval || 900) / 60)}m`, true]);
  setHTML($("#providers"), prov.map(([n, on]) => `<li class="${on ? "on" : ""}"><span class="dot"></span>${n}</li>`).join(""));
  $("#pullBtn").hidden = !(providers.sheets && providers.source_sheet);
  $("#pushBtn").hidden = !(providers.sheets && providers.dest_sheet);
  $("#baselineBtn").hidden = !(providers.sheets && providers.source_sheet);
  renderAutomation(st.scheduler);
  updatePipeline(st);
  const cycle = !!(st.scheduler && st.scheduler.running);
  if (running || cycle) startPolling();
  else if (!_rescreening && stats.queued === 0 && stats.processing === 0) stopPolling();
  return running;
}

// ── Sidebar shortcuts to the Google Sheets in use ────────────────────────
const _SHEETS_ICON = `<svg class="gs-ico" viewBox="0 0 24 32" aria-hidden="true"><path d="M2 0h14l8 8v22a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2V2a2 2 0 0 1 2-2z" fill="#0F9D58"/><path d="M16 0l8 8h-6a2 2 0 0 1-2-2z" fill="#87CEAC"/><path d="M5 13h14v12H5z" fill="#fff"/><path d="M5 17h14M5 21h14M11 13v12" stroke="#0F9D58" stroke-width="1.4"/></svg>`;
function renderSheetLinks(links) {
  setHTML($("#sheetLinks"), links.map((s) => `<a class="sheet-btn" href="${esc(s.url)}" target="_blank" rel="noopener" title="Open “${esc(s.name)}” in Google Sheets">${_SHEETS_ICON}<span class="sb-txt"><span class="sb-name">${esc(s.name)}</span><span class="sb-role">${esc(s.role)}</span></span><span class="sb-ext">↗</span></a>`).join(""));
}

// ── Team on duty: untick an AE on leave -> no new leads for them ─────────
function renderTeam(aes, off) {
  const offSet = new Set(off.map((x) => x.toLowerCase()));
  setHTML($("#teamOnDuty"), aes.map((a) => { const on = !offSet.has(a.toLowerCase());
    return `<li class="${on ? "on" : "off"}"><label title="${on ? "On duty — gets new leads. Untick if on leave." : "On leave — gets no new leads. Tick when back."}"><input type="checkbox" data-ae-duty="${esc(a)}" ${on ? "checked" : ""}/> ${esc(a)}${on ? "" : ' <span class="leave-tag">on leave</span>'}</label></li>`; }).join(""));
  const n = aes.filter((a) => !offSet.has(a.toLowerCase())).length;
  $("#teamNote").textContent = !aes.length ? "" : n === 0 ? "Nobody on duty — new leads wait without an AE." : n < aes.length ? `${aes.length - n} on leave · new leads go to the other ${n}` : "";
}
document.addEventListener("change", async (e) => {
  const cb = e.target.closest && e.target.closest("[data-ae-duty]"); if (!cb) return;
  cb.disabled = true;
  try { await readResponse(await fetch("/api/aes/availability", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: cb.dataset.aeDuty, available: cb.checked }) })); loadStats(); }
  catch (err) { alert(err.message); cb.checked = !cb.checked; }
  finally { cb.disabled = false; }
});

// ── Automation status ─────────────────────────────────────────────────────
let _autoNextAt = null;
function _ago(t) { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; }
function _lastRunText(l) {
  if (!l) return "no run yet";
  if (l.error) return `last run ${_ago(l.finished)} failed: ${l.error}`;
  const p = [];
  if (l.baselined != null) p.push(`marked ${l.baselined} existing rows as seen (backlog skipped)`);
  if (l.pulled != null) p.push(`${l.pulled} new lead${l.pulled === 1 ? "" : "s"}`);
  if (l.added || l.updated) p.push(`sheet: ${l.added || 0} added, ${l.updated || 0} updated`);
  else if (l.added == null) p.push("not pushed (push paused)");
  if (l.in_review) p.push(`${l.in_review} awaiting review`);
  if (l.report && l.report.changed) p.push(`GTM report: ${l.report.changed} cell(s) recounted`);
  if (l.marked_in_progress) p.push(`${l.marked_in_progress} marked In Progress`);
  if (l.report_issue) p.push(`GTM report issue: ${l.report_issue}`);
  if (l.incomplete) p.push(`${l.incomplete} incomplete held back`);
  return `last run ${_ago(l.finished)}${l.trigger === "manual" ? " (resumed)" : ""}: ${p.join(" · ") || "nothing new"}`;
}
let _autoPushing = false;   // push automation playing -> manual Push is redundant
function renderAutomation(a) {
  const el = $("#autoBar"); if (!el || !a) return;
  el.hidden = false;
  _autoPushing = !!(a.enabled && !a.push_paused);
  const pb = $("#pushBtn");
  if (pb) { pb.classList.toggle("is-auto", _autoPushing); pb.title = _autoPushing ? "Automation is pushing leads to the sheet automatically" : "Push decided leads to the sheet now"; }
  if (!a.enabled) { _autoNextAt = null; setHTML(el, `<span class="auto-dot"></span><b>Automation off</b><span class="sub">Set SCHEDULER_ENABLED=true on Render to run every ${Math.round(a.interval / 60)} min.</span>`); return; }
  const min = Math.round(a.interval / 60), both = a.paused && a.push_paused;
  _autoNextAt = a.running || both || a.next_in == null ? null : Date.now() + a.next_in * 1000;
  const disk = a.persistent_disk ? "" : `<span class="auto-warn">⚠ No persistent disk (DATA_DIR not set) — data resets on deploy</span>`;
  const toggle = (target, paused) => paused
    ? `<button class="auto-btn" data-target="${target}" data-pause="0" title="Resume ${target}: runs now, then every ${min} min">▶ ${target === "pull" ? "Pull" : "Push"}</button>`
    : `<button class="auto-btn on" data-target="${target}" data-pause="1" title="Pause ${target}">⏸ ${target === "pull" ? "Pull" : "Push"}</button>`;
  const part = (label, paused) => `${label} <b class="${paused ? "st-off" : "st-on"}">${paused ? "paused" : "on"}</b>`;
  const title = both ? "<b>Automation paused</b>" : !a.paused && !a.push_paused ? "<b>Automation on</b>" : "<b>Automation partly on</b>";
  const when = both ? (a.running ? " · finishing the current cycle…" : "") : ` · every ${min} min · <span id="autoNext">${a.running ? "running now…" : ""}</span>`;
  setHTML(el, `${toggle("pull", a.paused)}${toggle("push", a.push_paused)}<span class="auto-dot ${both ? "paused" : "on"}"></span>${title}${when}` +
    `<span class="sub">${part("pull", a.paused)} · ${part("push to sheet", a.push_paused)}${a.push_paused ? " — use ↑ Push" : ""}</span>` +
    `<span class="sub">${esc(_lastRunText(a.last))}</span>${disk}`);
  tickAutomation();
}
$("#autoBar").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-pause]"); if (!b) return;
  b.disabled = true;
  try { const a = await readResponse(await fetch("/api/automation/pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target: b.dataset.target, paused: b.dataset.pause === "1" }) })); renderAutomation(a); setTimeout(loadStats, 1500); }
  catch (err) { alert(err.message); b.disabled = false; }
});
function tickAutomation() {
  const n = $("#autoNext"); if (!n || _autoNextAt == null) return;
  const s = Math.max(0, Math.round((_autoNextAt - Date.now()) / 1000));
  n.textContent = s ? `next run in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "starting…";
}
setInterval(tickAutomation, 1000);
setInterval(loadStats, 30000);   // keep the automation status fresh while idle

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
// Each chart has its own period (remembered per browser). Rows come per lead:
// [day, channel, status, tier, owner]; day = the lead's date.
let _anRows = null;
// Periods offered on every chart (and in the toolbar, for all charts at once).
const _RANGES = [["today", "Today"], ["yesterday", "Yesterday"], ["3", "Last 3 days"], ["7", "Last 7 days"],
  ["14", "Last 14 days"], ["30", "Last 30 days"], ["week", "This week"], ["lastweek", "Last week"],
  ["month", "This month"], ["lastmonth", "Last month"], ["all", "All time"]];
const _dayAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return _isoDay(d); };
function _span(r) {                   // [first day, last day] of a period; weeks start on Sunday
  const now = new Date(), dow = now.getDay();
  if (r === "today") return [_dayAgo(0), _dayAgo(0)];
  if (r === "yesterday") return [_dayAgo(1), _dayAgo(1)];
  if (r === "week") return [_dayAgo(dow), _dayAgo(0)];
  if (r === "lastweek") return [_dayAgo(dow + 7), _dayAgo(dow + 1)];
  if (r === "month") return [_isoDay(new Date(now.getFullYear(), now.getMonth(), 1)), _dayAgo(0)];
  if (r === "lastmonth") return [_isoDay(new Date(now.getFullYear(), now.getMonth() - 1, 1)), _isoDay(new Date(now.getFullYear(), now.getMonth(), 0))];
  if (r === "all") return [null, _dayAgo(0)];
  return [_dayAgo(+r - 1), _dayAgo(0)];
}
const _range = { channels: "7", outcomes: "7", tiers: "7", owners: "7", time: "7" };
try { Object.assign(_range, JSON.parse(localStorage.getItem("ls-ranges") || "{}")); } catch {}
Object.keys(_range).forEach((k) => { if (!_RANGES.some(([v]) => v === _range[k])) _range[k] = "7"; });
document.querySelectorAll(".range-sel").forEach((s) => {
  s.innerHTML = _RANGES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  s.value = _range[s.dataset.chart] || "7";
  s.addEventListener("change", () => { _range[s.dataset.chart] = s.value; try { localStorage.setItem("ls-ranges", JSON.stringify(_range)); } catch {} syncRangeAll(); drawAnalytics(); });
});
$("#rangeAll").innerHTML = _RANGES.map(([v, l]) => `<option value="${v}">All charts: ${l.toLowerCase()}</option>`).join("")
  + `<option value="mixed" hidden>Charts: mixed periods</option>`;
function syncRangeAll() {           // the toolbar picker shows the shared period, if any
  const v = new Set(Object.values(_range)); $("#rangeAll").value = v.size === 1 ? [...v][0] : "mixed";
}
syncRangeAll();
$("#rangeAll").addEventListener("change", (e) => {
  if (e.target.value === "mixed") return;
  Object.keys(_range).forEach((k) => (_range[k] = e.target.value));
  document.querySelectorAll(".range-sel").forEach((s) => (s.value = e.target.value));
  try { localStorage.setItem("ls-ranges", JSON.stringify(_range)); } catch {}
  drawAnalytics();
});
const _inRange = (chart) => { const [from, to] = _span(_range[chart]); return _anRows.filter((x) => (!from || x[0] >= from) && x[0] <= to); };
const _tally = (rows, i, skip = "") => rows.reduce((m, x) => { if (x[i] && x[i] !== skip) m[x[i]] = (m[x[i]] || 0) + 1; return m; }, {});
async function loadAnalytics() {
  if (!_ready()) { setTimeout(loadAnalytics, 400); return; }
  let a; try { a = await (await fetch("/api/analytics")).json(); } catch { return; }
  _anRows = a.rows || [];
  drawAnalytics();
}
function drawAnalytics() {
  if (!_anRows) return;
  const barOf = (m) => { const k = Object.keys(m).sort((x, y) => m[y] - m[x]); return _bar(k.length ? k : ["—"], k.length ? k.map((x) => m[x]) : [0]); };
  _draw("chChannels", barOf(_tally(_inRange("channels"), 1)));
  _draw("chTiers", barOf(_tally(_inRange("tiers"), 3)));
  _draw("chOwners", barOf(_tally(_inRange("owners").filter((x) => x[2] === "accepted"), 4)));
  // Leads over time: every day in the period, zero days included.
  const tr = _inRange("time"), perDay = _tally(tr, 0);
  const [from, to] = _span(_range.time), first = from || Object.keys(perDay).sort()[0] || to;
  const labels = []; for (let d = new Date(first + "T00:00"); _isoDay(d) <= to; d.setDate(d.getDate() + 1)) labels.push(_isoDay(d));
  _draw("chTime", _line(labels.map((d) => d.slice(5)), labels.map((d) => perDay[d] || 0)));
  const a = { statuses: _tally(_inRange("outcomes"), 2) };
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
  // sparklines + total trend (always the last 14 days): stored for loadStats,
  // re-render tiles only if changed
  const all = _tally(_anRows, 0), series = [...Array(14)].map((_, i) => all[_dayAgo(13 - i)] || 0);
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
    <div class="sub">${badge(lead.status, lead.stage)}${lead.channel ? ` · ${esc(lead.channel)}` : ""}</div>
    ${lead.status === "duplicate" ? `<div class="dup-note">${lead.prior_status && lead.prior_status !== "queued" ? `Was <b>${esc(lead.prior_status)}</b>, then merged` : "Merged on arrival"} into lead #${lead.merged_into} (${esc(lead.dup_reason || "")}). Change this person's AE on the Leads table or in lead #${lead.merged_into}.</div>` : `<div class="ae-pick"><span class="k">AE</span><select id="aeSelect"><option value="">${lead.owner ? "" : "— not assigned yet —"}</option>${_aes.map((a) => `<option ${a === lead.owner ? "selected" : ""}>${esc(a)}</option>`).join("")}</select><span id="aeMsg" class="sub"></span></div>`}
    ${(lead.merged_from || []).length ? `<div class="dup-note">Combined with duplicate lead${lead.merged_from.length > 1 ? "s" : ""} ${lead.merged_from.map((x) => "#" + x).join(", ")} (same person from another channel).</div>` : ""}
    ${lead.dup_of && lead.status !== "duplicate" ? `<div class="dup-note warn">Possible duplicate of #${lead.dup_of} (${esc(lead.dup_reason || "")}) — both leads are held back from the Pipedrive sheet. Decide on the <a href="#duplicates" onclick="closeDrawer()">Duplicates</a> page.</div>` : ""}
    ${screen}<h3>Raw input</h3><div class="kv">${kv("Company", lead.company)}${kv("Email", lead.email)}${kv("Phone", lead.phone)}${kv("LinkedIn", lead.li_optout ? "Lead says they don't have one (LinkedIn search skipped, not screened)" : lead.linkedin)}${kv("Link given", lead.other_link)}${lead.link_raw && lead.link_raw !== lead.linkedin && lead.link_raw !== lead.other_link ? kv("Typed as", lead.link_raw) : ""}${kv("Visa", lead.visa)}${kv("Heard about us", lead.heard_about)}${kv("Message", lead.message)}</div>
    ${ex.location || ex.role_guess || ex.school || ex.notes ? `<h3>Extracted &amp; enriched</h3><div class="kv">${kv("Company", ex.company)}${kv("School", ex.school)}${kv("Location", ex.location)}${kv("Role guess", ex.role_guess)}${kv("Notes", ex.notes)}</div>${(ex.keywords || []).length ? `<div class="tags" style="margin-top:8px">${ex.keywords.map((k) => `<span class="tag">${esc(k)}</span>`).join("")}</div>` : ""}` : ""}
    ${lead.queries ? `<h3>Search queries</h3><div class="tags">${lead.queries.map((q) => `<span class="tag">${esc(q)}</span>`).join("")}</div>` : ""}
    ${lead.error ? `<h3>Error</h3><div class="cand-reason" style="color:var(--bad)">${esc(lead.error)}</div>` : ""}
    <h3>Candidates (${cands.length})</h3>${candHtml}
    <div class="row-actions" style="justify-content:flex-start;margin-top:14px">${lead.status === "review" || lead.status === "accepted" ? `<button class="mini no" id="rejectBtn">Reject lead</button>` : ""}<button class="mini no" id="deleteBtn">Delete lead</button></div>`;
  $("#drawerBody").querySelectorAll("[data-choose]").forEach((b) => b.addEventListener("click", async () => { await fetch(`/api/leads/${id}/choose`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_index: +b.dataset.choose }) }); closeDrawer(); refresh(); }));
  const rej = $("#rejectBtn"); if (rej) rej.addEventListener("click", async () => { await fetch(`/api/leads/${id}/reject`, { method: "POST" }); closeDrawer(); refresh(); });
  $("#deleteBtn").addEventListener("click", async () => { if (await deleteLead(id)) closeDrawer(); });
  const sel = $("#aeSelect");
  if (sel) sel.addEventListener("change", async () => {
    if (!sel.value) return; const msg = $("#aeMsg"); msg.textContent = "Saving…";
    try {
      const d = await readResponse(await fetch(`/api/leads/${id}/owner`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ owner: sel.value }) }));
      msg.textContent = d.synced ? "Saved — updating the Pipedrive sheet and GTM report now." : "Saved.";
      refresh();
    } catch (e) { msg.textContent = e.message; }
  });
  $("#drawer").classList.add("open");
}
function closeDrawer() { $("#drawer").classList.remove("open"); }
$("#drawerClose").addEventListener("click", closeDrawer);
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeDrawer(); });

// ── Boot ────────────────────────────────────────────────────────────────────
function startPolling() { if (!pollTimer) pollTimer = setInterval(refresh, 2000); }
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }
function refresh() { loadStats(); loadLeads(); loadReview(); loadDuplicates(); loadAnalytics(); }
setTheme(document.documentElement.dataset.theme || "dark");
setPage(location.hash.slice(1) || "dashboard");
refresh();
loadStats().then((r) => { if (r) startPolling(); });

// ── Pipedrive: preview (read-only) + write the ticked leads ─────────────────
const _PD_LABEL = { create: "New deal", link: "Has a deal already", update: "Changed — update", unchanged: "Up to date", skipped: "Not ready", error: "Error" };
const _PD_DONE = { create: "Deal created", link: "Linked", update: "Updated", error: "Error" };
const _PD_WRITABLE = new Set(["create", "link", "update"]);
let _pdPlan = null;
// Segments + filters. The selection is kept by lead (row|key), so switching a
// segment or filter never loses what's ticked; Write on Pipe writes all of it.
const _pdSel = new Set(), _pdF = { seg: "all", when: "all", ae: "all", source: "all", newOnly: false };
let _pdNew = new Set();
const pdId = (e) => `${e.row}|${e.key}`;
const pdDeal = (e, domain) => e.deal_id && domain ? `<a href="https://${esc(domain)}.pipedrive.com/deal/${e.deal_id}" target="_blank">${esc(e.title)} ↗</a>` : esc(e.title);
const pdWhat = (e) => `<div class="subtle">${e.actions.map(esc).join("<br>")}${e.notes.length ? "<br>⚠ " + e.notes.map(esc).join("<br>⚠ ") : ""}</div>`;
function pdWhen(day) {                        // weeks start on Sunday, like the GTM report
  const now = new Date(), y = new Date(now), ws = new Date(now), lws = new Date(now);
  y.setDate(now.getDate() - 1); ws.setDate(now.getDate() - now.getDay()); lws.setDate(ws.getDate() - 7);
  const tags = [];
  if (!day) return ["older"];
  if (day === _isoDay(now)) tags.push("today");
  if (day === _isoDay(y)) tags.push("yesterday");
  if (day >= _isoDay(ws)) tags.push("week");
  else if (day >= _isoDay(lws)) tags.push("lastweek");
  else tags.push("older");
  return tags;
}
function pdMatch(e, ignoreSeg) {
  const f = _pdF;
  if (!ignoreSeg && f.seg !== "all" && e.status !== f.seg) return false;
  if (f.when !== "all" && !pdWhen(e.day).includes(f.when)) return false;
  if (f.ae !== "all" && (e.owner || "—") !== f.ae) return false;
  if (f.source !== "all" && (e.source || "—") !== f.source) return false;
  if (f.newOnly && !_pdNew.has(pdId(e))) return false;
  return true;
}
function pdOptions(sel, label, values, cur) {
  setHTML(sel, `<option value="all">All ${label}</option>` + values.map((v) => `<option value="${esc(v)}"${v === cur ? " selected" : ""}>${esc(v)}</option>`).join(""));
  sel.value = values.includes(cur) ? cur : "all";
}
function pdRender(d) {
  if (d) {
    _pdPlan = d;
    const live = new Set(d.plan.filter((e) => _PD_WRITABLE.has(e.status)).map(pdId));
    [..._pdSel].forEach((k) => { if (!live.has(k)) _pdSel.delete(k); });
    pdOptions($("#pdAe"), "AEs", [...new Set(d.plan.map((e) => e.owner || "—"))].sort(), _pdF.ae);
    pdOptions($("#pdSource"), "sources", [...new Set(d.plan.map((e) => e.source || "—"))].sort(), _pdF.source);
    _pdF.ae = $("#pdAe").value; _pdF.source = $("#pdSource").value;
    $("#pdFilters").hidden = $("#pdSelbar").hidden = false;
  }
  d = _pdPlan; if (!d) return;
  const todo = d.plan.map((e, i) => ({ ...e, i })).filter((e) => e.status !== "unchanged");
  // Segment chips count what the other filters leave, so the numbers match the table.
  const base = todo.filter((e) => pdMatch(e, true)), by = {};
  base.forEach((e) => (by[e.status] = (by[e.status] || 0) + 1));
  const chip = (k, label, n) => `<button class="chan-chip${_pdF.seg === k ? " active" : ""}" data-seg="${k}" role="tab" aria-selected="${_pdF.seg === k}">${label} <span class="n">${n}</span></button>`;
  setHTML($("#pdSegs"), chip("all", "All", base.length) + ["create", "link", "update", "skipped", "error"].filter((k) => by[k] || _pdF.seg === k).map((k) => chip(k, _PD_LABEL[k], by[k] || 0)).join(""));
  $("#pdNewN").textContent = _pdNew.size ? `(${todo.filter((e) => _pdNew.has(pdId(e))).length})` : "";
  const synced = d.plan.length - todo.length;
  setHTML($("#pdSynced"), synced ? `${synced} lead(s) already synced and unchanged are hidden here: see <a href="#written">Written on Pipedrive</a>.` : "");
  const shown = todo.filter((e) => pdMatch(e));
  setHTML($("#pdRows"), shown.map((e) => `<tr><td>${_PD_WRITABLE.has(e.status) ? `<input type="checkbox" class="pd-pick" data-id="${esc(pdId(e))}"${_pdSel.has(pdId(e)) ? " checked" : ""} />` : ""}</td><td>${e.row}</td><td><b>${esc(e.name)}</b>${_pdNew.has(pdId(e)) ? '<span class="pd-new">NEW</span>' : ""}<div class="subtle">${esc(e.email)}${e.day ? " · " + esc(e.day) : ""}${e.source ? " · " + esc(e.source) : ""}</div></td><td>${pdDeal(e, d.domain)}</td><td>${esc(e.owner)}</td>
    <td><span class="pd-tag pd-${e.status}">${_PD_LABEL[e.status] || e.status}</span>${pdWhat(e)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="empty">${!d.plan.length ? "The Pipedrive sheet is empty." : !todo.length ? "Everything in the sheet is already synced. 🎉" : "No leads match these filters."}</td></tr>`);
  const nNew = base.filter((e) => e.status === "create").length, nLink = base.filter((e) => e.status === "link").length;
  $("#pdPickNew").textContent = `Select all new deals (${nNew})`; $("#pdPickNew").disabled = !nNew;
  $("#pdPickLink").textContent = `Select all with a deal (${nLink})`; $("#pdPickLink").disabled = !nLink;
  const vis = shown.filter((e) => _PD_WRITABLE.has(e.status));
  $("#pdAll").checked = vis.length > 0 && vis.every((e) => _pdSel.has(pdId(e))); $("#pdAll").disabled = !vis.length;
  pdPicked(shown.length, todo.length);
}
function pdPicked(shown, total) {
  const n = _pdSel.size, b = $("#pdWriteBtn");
  b.disabled = !n; b.textContent = n ? `Write on Pipe (${n})` : "Write on Pipe";
  if (shown !== undefined) $("#pdShown").textContent = `Showing ${shown} of ${total}` + (n ? ` · ${n} selected` : "");
  else if (_pdPlan) $("#pdShown").textContent = $("#pdShown").textContent.replace(/ · \d+ selected$/, "") + (n ? ` · ${n} selected` : "");
}
async function pdPreview(note) {
  const b = $("#pdPreviewBtn"), m = $("#pdMsg"); b.disabled = true;
  if (!note) { m.className = "msg"; m.textContent = "Reading the sheet and Pipedrive (read-only)…"; }
  try {
    const d = await readResponse(await fetch("/api/pipedrive/preview"));
    let prev = null; try { prev = JSON.parse(localStorage.getItem("ls-pd-seen") || "null"); } catch {}
    if (!note) {
      _pdNew = new Set(prev ? d.plan.map(pdId).filter((k) => !prev.includes(k)) : []);
      try { localStorage.setItem("ls-pd-seen", JSON.stringify(d.plan.map(pdId))); } catch {}
    }
    if (!note) { m.className = "msg ok"; m.textContent = `Preview only, nothing written · pipeline “${d.pipeline}” → stage “${d.stage}”` + (d.missing_fields.length ? ` · not in Pipedrive: ${d.missing_fields.join(", ")}` : ""); }
    pdRender(d);
  } catch (e) { m.className = "msg err"; m.textContent = (note ? note + " · " : "") + e.message; }
  finally { b.disabled = false; }
}
async function pdLoadWritten() {
  let d; try { d = await (await fetch("/api/pipedrive/written")).json(); } catch { return; }
  $("#writtenCount").textContent = d.written.length;
  setHTML($("#pdWritten"), d.written.map((e) => `<tr><td>${new Date(e.at * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td><td><b>${esc(e.name)}</b><div class="subtle">${esc(e.email)}</div></td><td>${pdDeal(e, d.domain)}</td><td>${esc(e.owner)}</td>
    <td><span class="pd-tag pd-${e.status}">${_PD_DONE[e.status] || e.status}</span>${pdWhat(e)}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">Nothing written yet.</td></tr>`);
}
$("#pdRows").addEventListener("change", (e) => {
  const c = e.target; if (!c.classList.contains("pd-pick")) return;
  c.checked ? _pdSel.add(c.dataset.id) : _pdSel.delete(c.dataset.id); pdRender();
});
$("#pdAll").addEventListener("change", (e) => {   // ticks / unticks the rows on screen
  document.querySelectorAll(".pd-pick").forEach((c) => (e.target.checked ? _pdSel.add(c.dataset.id) : _pdSel.delete(c.dataset.id)));
  pdRender();
});
$("#pdSegs").addEventListener("click", (e) => { const b = e.target.closest("[data-seg]"); if (b) { _pdF.seg = b.dataset.seg; pdRender(); } });
[["#pdWhen", "when"], ["#pdAe", "ae"], ["#pdSource", "source"]].forEach(([s, k]) => $(s).addEventListener("change", (e) => { _pdF[k] = e.target.value; pdRender(); }));
$("#pdNewOnly").addEventListener("change", (e) => { _pdF.newOnly = e.target.checked; pdRender(); });
$("#pdSelbar").addEventListener("click", (e) => {   // select by kind, within the date/AE/source filters
  const b = e.target.closest("[data-pick]"); if (!b || !_pdPlan) return;
  if (b.dataset.pick === "none") _pdSel.clear();
  else _pdPlan.plan.filter((x) => x.status === b.dataset.pick && pdMatch(x, true)).forEach((x) => _pdSel.add(pdId(x)));
  pdRender();
});
$("#pdPreviewBtn").addEventListener("click", () => pdPreview());
// In-page confirm (the desktop app's browser blocks window.confirm, which made
// the button silently do nothing). Resolves true on "Write on Pipe".
function pdConfirm(picks) {
  const nNew = picks.filter((e) => e.status === "create").length;
  $("#pdConfirmTitle").textContent = `Write ${picks.length} lead(s) to Pipedrive?`;
  setHTML($("#pdConfirmBody"), `<ul>${picks.map((e) => `<li><b>${esc(e.name)}</b>: ${_PD_LABEL[e.status]}${e.status === "create" ? ` “${esc(e.title)}” → ${esc(e.owner)}` : ""}</li>`).join("")}</ul>`
    + (nNew ? `<p class="subtle">This creates <b>${nNew}</b> deal(s) in Pipedrive.</p>` : ""));
  const box = $("#pdConfirm"); box.hidden = false; $("#pdConfirmYes").focus();
  return new Promise((resolve) => {
    const done = (ok) => { box.hidden = true; $("#pdConfirmYes").onclick = $("#pdConfirmNo").onclick = box.onclick = null; resolve(ok); };
    $("#pdConfirmYes").onclick = () => done(true);
    $("#pdConfirmNo").onclick = () => done(false);
    box.onclick = (ev) => { if (ev.target === box) done(false); };
  });
}
function pdProgress(j) {
  const total = j.total || 0, done = Math.min(j.done || 0, total);
  $("#pdProgress").hidden = false;
  $("#pdProgText").textContent = !j.running ? "Done" : j.current ? `Writing ${j.current}…` : "Preparing (reading Pipedrive)…";
  $("#pdProgNum").textContent = total ? `${done} of ${total}` : "";
  $("#pdBarFill").style.width = (total ? Math.round((done / total) * 100) : 0) + "%";
}
$("#pdWriteBtn").addEventListener("click", async () => {
  const picks = _pdPlan.plan.filter((e) => _pdSel.has(pdId(e)));
  if (!picks.length || !(await pdConfirm(picks))) return;
  const b = $("#pdWriteBtn"), m = $("#pdMsg"); b.disabled = true; $("#pdPreviewBtn").disabled = true; m.className = "msg"; m.textContent = "";
  try {
    let j = await readResponse(await fetch("/api/pipedrive/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rows: picks.map((e) => ({ row: e.row, key: e.key })) }) }));
    pdProgress(j);
    while (j.running) { await new Promise((r) => setTimeout(r, 700)); j = await readResponse(await fetch("/api/pipedrive/job")); pdProgress(j); }
    if (j.error) throw new Error(j.error);
    const d = j.result, bad = d.counts.error || 0;
    const note = `Written to Pipedrive: ${picks.length - bad} lead(s)` + (bad ? `, ${bad} failed` : "") + " · details on the “Written on Pipedrive” page";
    m.className = bad ? "msg err" : "msg ok"; m.textContent = note;
    pdLoadWritten();
    _pdSel.clear();
    await pdPreview(note);          // refresh the list: written leads drop out of it
  } catch (e) { m.className = "msg err"; m.textContent = e.message; pdPicked(); $("#pdPreviewBtn").disabled = false; }
  finally { setTimeout(() => ($("#pdProgress").hidden = true), 2500); }
});
pdLoadWritten();

// ── GTM report: last recount, week by week, lead by lead ───────────────────
let _gtm = null, _gtmWeek = "", _gtmKind = "all";
const _GTM_KIND = { counted: "Counted", "re-engagement": "Re-engagement (not counted)", repeat: "Same person again (not counted)" };
const _CHAN_TAB = { Meta: "Meta form", WhatsApp: "WhatsApp chatbot", Website: "Website form" };
function gtmRender() {
  const d = _gtm; if (!d || !d.weeks) { setHTML($("#gtmRows"), `<tr><td colspan="5" class="empty">No recount yet. Click “Recount now”.</td></tr>`); return; }
  const weeks = Object.keys(d.weeks).sort().reverse();
  if (!weeks.includes(_gtmWeek)) _gtmWeek = weeks[0];
  const fmt = (iso) => new Date(iso + "T00:00").toLocaleDateString([], { day: "numeric", month: "short" });
  setHTML($("#gtmWeeks"), weeks.map((w, i) => `<button class="chan-chip${w === _gtmWeek ? " active" : ""}" data-week="${w}" role="tab">${i === 0 ? "This week" : "Last week"} <span class="n">from ${fmt(w)}</span></button>`).join(""));
  const c = d.weeks[_gtmWeek] || {}, rows = d.rows || Object.keys(c);
  const inbound = rows.filter((r) => !r.endsWith(" - SQL")), ae = rows.filter((r) => r.endsWith(" - SQL"));
  const total = inbound.reduce((s, r) => s + (c[r] || 0), 0);
  const cell = (k, v, cls = "") => `<div class="gtm-cell ${cls}"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`;
  setHTML($("#gtmNumbers"), cell("Inbound counted (all channels)", total, "total") + inbound.map((r) => cell(r, c[r] || 0)).join("") + ae.map((r) => cell(r, c[r] || 0, "ae")).join(""));
  const leads = (d.detail || {})[_gtmWeek] || [], by = {};
  leads.forEach((l) => (by[l.kind] = (by[l.kind] || 0) + 1));
  setHTML($("#gtmKinds"), [["all", "All", leads.length], ...Object.keys(_GTM_KIND).map((k) => [k, _GTM_KIND[k], by[k] || 0])].map(([k, label, n]) => `<button class="chan-chip${_gtmKind === k ? " active" : ""}" data-kind="${k}" role="tab">${label} <span class="n">${n}</span></button>`).join(""));
  const shown = leads.filter((l) => _gtmKind === "all" || l.kind === _gtmKind);
  setHTML($("#gtmRows"), shown.map((l) => `<tr><td>${esc(fmt(l.date))}</td><td><b>${esc(l.name)}</b><div class="subtle">${esc(l.email || l.phone)}${l.form ? " · " + esc(l.form) : ""}</div></td><td>${esc(_CHAN_TAB[l.channel] || l.channel)}${l.heard ? `<div class="subtle">${esc(l.heard)}</div>` : ""}</td>
    <td>${l.kind === "counted" ? esc(l.row) : `<span class="gtm-kind ${l.kind === "repeat" ? "repeat" : "reeng"}">${esc(_GTM_KIND[l.kind])}</span>`}</td><td>${l.kind === "counted" ? esc(l.ae || "Not distributed") : ""}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">No leads here.</td></tr>`);
}
async function gtmLoad() {
  try { _gtm = await readResponse(await fetch("/api/gtm")); } catch (e) { $("#gtmMsg").className = "msg err"; $("#gtmMsg").textContent = e.message; return; }
  if (_gtm.sheet_id) { $("#gtmSheet").href = `https://docs.google.com/spreadsheets/d/${_gtm.sheet_id}`; $("#gtmSheet").hidden = false; }
  if (_gtm.at) { $("#gtmMsg").className = "msg"; $("#gtmMsg").textContent = `Last recounted ${new Date(_gtm.at * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} · the automation recounts every cycle.`; }
  gtmRender();
}
$("#gtmWeeks").addEventListener("click", (e) => { const b = e.target.closest("[data-week]"); if (b) { _gtmWeek = b.dataset.week; gtmRender(); } });
$("#gtmKinds").addEventListener("click", (e) => { const b = e.target.closest("[data-kind]"); if (b) { _gtmKind = b.dataset.kind; gtmRender(); } });
$("#gtmRecount").addEventListener("click", async () => {
  const b = $("#gtmRecount"), m = $("#gtmMsg"); b.disabled = true; m.className = "msg"; m.textContent = "Recounting from the lead sheet and Pipedrive…";
  try { const d = await readResponse(await fetch("/api/gtm/recount", { method: "POST" })); _gtm = d; gtmRender(); m.className = "msg ok"; m.textContent = d.changed ? `Recounted · ${d.changed} cell(s) updated in the GTM sheet.` : "Recounted · the GTM sheet was already up to date."; }
  catch (e) { m.className = "msg err"; m.textContent = e.message; }
  finally { b.disabled = false; }
});
gtmLoad();
