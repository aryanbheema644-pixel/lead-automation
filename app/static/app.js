const $ = (s) => document.querySelector(s);
let currentView = "all";
let pollTimer = null;

async function readResponse(r) {
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); }
  catch { data = { detail: text.slice(0, 300) || `HTTP ${r.status}` }; }
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// ── Upload ──────────────────────────────────────────────────────────────────
const fileInput = $("#fileInput"), filedrop = $("#filedrop");
filedrop.addEventListener("dragover", (e) => { e.preventDefault(); filedrop.classList.add("drag"); });
filedrop.addEventListener("dragleave", () => filedrop.classList.remove("drag"));
filedrop.addEventListener("drop", (e) => { e.preventDefault(); filedrop.classList.remove("drag"); if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]); });
fileInput.addEventListener("change", () => { if (fileInput.files.length) uploadFile(fileInput.files[0]); });

async function uploadFile(file) {
  const msg = $("#uploadMsg"); msg.className = "msg"; msg.textContent = `Uploading ${file.name}…`;
  const fd = new FormData(); fd.append("file", file);
  try {
    const data = await readResponse(await fetch("/api/upload", { method: "POST", body: fd }));
    msg.className = "msg ok";
    msg.textContent = `Added ${data.inserted} leads. Click “Run pipeline”.`;
    refresh();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}

// ── Actions ─────────────────────────────────────────────────────────────────
$("#runBtn").addEventListener("click", async () => {
  const btn = $("#runBtn"); btn.disabled = true;
  try {
    const data = await readResponse(await fetch("/api/run", { method: "POST" }));
    $("#uploadMsg").className = "msg ok";
    $("#uploadMsg").textContent = `Processing ${data.queued} queued leads…`;
    startPolling();
  } catch (e) { $("#uploadMsg").className = "msg err"; $("#uploadMsg").textContent = e.message; }
  finally { setTimeout(() => (btn.disabled = false), 1500); }
});
$("#resetBtn").addEventListener("click", async () => {
  if (!confirm("Delete all leads and results?")) return;
  await fetch("/api/reset", { method: "POST" }); refresh();
});
$("#pullBtn").addEventListener("click", async () => {
  const msg = $("#uploadMsg"); msg.className = "msg"; msg.textContent = "Pulling leads from the source sheet…";
  try {
    const data = await readResponse(await fetch("/api/sheets/pull", { method: "POST" }));
    const bd = data.tabs ? " (" + Object.entries(data.tabs).map(([t, n]) => `${t}: ${n}`).join(", ") + ")" : "";
    msg.className = "msg ok"; msg.textContent = `Pulled ${data.inserted} leads${bd}. Click “Run pipeline”.`; refresh();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
});
$("#pushBtn").addEventListener("click", async () => {
  const msg = $("#uploadMsg"); msg.className = "msg"; msg.textContent = "Pushing refined leads to the destination sheet…";
  try {
    const data = await readResponse(await fetch("/api/sheets/push", { method: "POST" }));
    msg.className = "msg ok"; msg.textContent = data.pushed ? `Pushed ${data.pushed} leads to the destination sheet.` : (data.note || "Nothing new to push.");
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
});

// ── Tabs ────────────────────────────────────────────────────────────────────
$("#tabs").addEventListener("click", (e) => {
  const t = e.target.closest(".tab"); if (!t) return;
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  t.classList.add("active"); currentView = t.dataset.view;
  $("#viewAll").hidden = currentView !== "all";
  $("#viewReview").hidden = currentView !== "review";
  loadLeads();
});

// ── Pipeline animation ──────────────────────────────────────────────────────
// backend stage -> node index (1..8): Ingest Extract Queries Search Match Scrape Screen Output
const STAGE_NODE = {
  extracting: 2, querying: 3, searching: 4, matching: 5,
  "scraping profile": 6, screening: 7, done: 8, provided: 8, no_linkedin: 8,
};
function updatePipeline(status) {
  const pipe = $("#pipeline");
  const running = status.running && status.current;
  pipe.classList.toggle("running", !!running);
  const nodes = document.querySelectorAll("#snake .node");
  let active = 0;
  if (running) {
    active = STAGE_NODE[status.current.stage] || 1;
    $("#pipelineNow").textContent = `${status.current.stage || "working"} · ${status.current.name || ""}`.trim();
  } else {
    const s = status.stats || {};
    const processed = (s.accepted || 0) + (s.review || 0) + (s.rejected || 0);
    active = processed > 0 ? 8 : 0;  // full snake if anything is done, else empty
    $("#pipelineNow").textContent = processed > 0 ? `Idle · ${processed} processed` : "Idle";
  }
  nodes.forEach((n) => {
    const i = +n.dataset.i;
    n.classList.toggle("done", running ? i < active : i <= active);
    n.classList.toggle("active", running && i === active);
  });
  const doneFrac = active <= 1 ? 0 : ((active - 1) / 7) * 100;
  const dl = (!running && active === 8) ? 100 : doneFrac;
  $("#trackDone").setAttribute("stroke-dasharray", `${dl} 100`);
}

// ── Rendering helpers ───────────────────────────────────────────────────────
function badge(status, stage) {
  const label = status === "processing" && stage ? stage : status;
  const spin = status === "processing" ? '<span class="spin">◠</span>' : "";
  return `<span class="badge ${status}">${spin}${esc(label)}</span>`;
}
const SOURCE_LABELS = {
  linkedin: "LinkedIn profile", "linkedin (provided)": "LinkedIn (provided)",
  scholar: "Google Scholar / ResearchGate", github: "GitHub", company: "Company page",
  imdb: "IMDb", social: "Social profile", news: "News / article", pdf: "PDF document",
  "other link": "Other link", personal_or_other: "Personal site / other",
};
function prettyUrl(u) { u = String(u || "").replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/$/, ""); return u.length > 42 ? u.slice(0, 42) + "…" : u; }
function topMatch(lead) {
  const c = (lead.candidates || [])[0], ch = lead.chosen;
  const src = ch && ch.url ? ch : c;
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
async function acceptTop(id) {
  await fetch(`/api/leads/${id}/choose`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_index: 0 }) });
  refresh();
}
async function rejectLead(id) { await fetch(`/api/leads/${id}/reject`, { method: "POST" }); refresh(); }

// ── Views ───────────────────────────────────────────────────────────────────
async function loadLeads() {
  if (currentView === "review") return loadReview();
  const { leads } = await (await fetch("/api/leads?status=all")).json();
  const tbody = $("#leadRows");
  if (!leads.length) { tbody.innerHTML = `<tr><td colspan="7" class="empty">No leads yet — upload a CSV or pull from your sheet.</td></tr>`; return; }
  tbody.innerHTML = leads.map((l) => `
    <tr>
      <td class="sub">${l.id}</td>
      <td class="name-cell">${esc(l.name || "—")}<div class="sub">${esc(l.email || "")}</div></td>
      <td>${esc(l.company || "—")}</td>
      <td>${badge(l.status, l.stage)}</td>
      <td>${topMatch(l)}</td>
      <td>${tierChip(l.screening)}</td>
      <td>${rowActions(l)}</td>
    </tr>`).join("");
  wireRowButtons(tbody);
}
async function loadReview() {
  const { leads } = await (await fetch("/api/leads?status=review")).json();
  const wrap = $("#reviewCards");
  if (!leads.length) { wrap.innerHTML = `<div class="review-empty">Nothing needs review — you're all caught up.</div>`; return; }
  wrap.innerHTML = leads.map((l) => {
    const ch = l.chosen || (l.candidates || [])[0] || {};
    const label = SOURCE_LABELS[ch.source_type] || ch.source_type || "—";
    return `
      <div class="review-card">
        <div class="rc-row"><h4>${esc(l.name || "Lead #" + l.id)}</h4>${tierChip(l.screening)}</div>
        <div class="rc-meta">${esc(l.company || l.email || "")}</div>
        <div><div class="tm-label">${esc(label)}</div>${ch.url ? `<a class="rc-url" href="${ch.url}" target="_blank" rel="noopener">${esc(prettyUrl(ch.url))}</a>` : ""}</div>
        <div class="cand-reason">${esc((l.reasoning || "").slice(0, 180))}</div>
        <div class="rc-actions">
          <button class="mini ok" data-accept="${l.id}">Accept</button>
          <button class="mini no" data-reject="${l.id}">Reject</button>
          <button class="link-btn" data-open="${l.id}">Details ▸</button>
        </div>
      </div>`;
  }).join("");
  wireRowButtons(wrap);
}

async function loadStats() {
  const st = await (await fetch("/api/status")).json();
  const { stats, running, providers } = st;
  const cells = [["total","Total"],["queued","Queued"],["processing","Processing"],["accepted","Accepted"],["review","Review"],["rejected","Rejected"]];
  $("#stats").innerHTML = cells.map(([k, lbl]) => `<div class="stat ${k}"><div class="num">${stats[k] || 0}</div><div class="lbl">${lbl}</div></div>`).join("");
  $("#reviewCount").textContent = stats.review || 0;
  const p = providers, prov = [["OpenRouter", p.openrouter, p.model], ["Tavily", p.tavily], ["Firecrawl", p.firecrawl], ["Apify", p.apify], ["Screening", p.screening], ["Sheets", p.sheets]];
  $("#providers").innerHTML = prov.map(([n, on, x]) => `<span class="pill ${on ? "on" : ""}" title="${x || ""}"><span class="dot"></span>${n}</span>`).join("");
  $("#pullBtn").hidden = !(p.sheets && p.source_sheet);
  $("#pushBtn").hidden = !(p.sheets && p.dest_sheet);
  updatePipeline(st);
  if (!running && (stats.queued === 0 && stats.processing === 0)) stopPolling();
  return running;
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
  const screeningHtml = hasScreen ? `
    <h3>Screening verdict (RAG fit-eval)</h3>
    ${scr.tier ? `<div class="sub" style="margin-bottom:8px"><span class="tier ${esc(scr.tier)}">${esc(scr.tier)}</span> ${esc(scr.confidence || "")}</div>` : ""}
    <div class="kv">${kv("Best path", scr.best_path)}${kv("Backup path", scr.backup_path)}${kv("Key strength", scr.key_strength)}${kv("Red flag", scr.red_flag)}${kv("Flip trigger", scr.flip_trigger)}${kv("Matched cases", scr.matched_cases)}${kv("Note", scr.note)}${kv("Error", scr.error)}</div>
    ${scr.answer ? `<div class="cand-reason" style="white-space:pre-wrap;margin-top:8px">${esc(scr.answer)}</div>` : ""}` : "";

  $("#drawerBody").innerHTML = `
    <h2>${esc(lead.name || "Lead #" + lead.id)}</h2>
    <div class="sub">${badge(lead.status, lead.stage)}${lead.owner ? ` · AE: <b>${esc(lead.owner)}</b>` : ""}</div>
    ${screeningHtml}
    <h3>Raw input</h3>
    <div class="kv">${kv("Company", lead.company)}${kv("Email", lead.email)}${kv("Phone", lead.phone)}${kv("LinkedIn", lead.linkedin)}${kv("Message", lead.message)}</div>
    ${ex.location || ex.role_guess || ex.school || ex.notes ? `<h3>Extracted &amp; enriched</h3><div class="kv">${kv("Company", ex.company)}${kv("School", ex.school)}${kv("Location", ex.location)}${kv("Role guess", ex.role_guess)}${kv("Notes", ex.notes)}</div>${(ex.keywords || []).length ? `<div class="tags" style="margin-top:8px">${ex.keywords.map(k => `<span class="tag">${esc(k)}</span>`).join("")}</div>` : ""}` : ""}
    ${lead.queries ? `<h3>Search queries</h3><div class="tags">${lead.queries.map(q => `<span class="tag">${esc(q)}</span>`).join("")}</div>` : ""}
    ${lead.error ? `<h3>Error</h3><div class="cand-reason" style="color:var(--red)">${esc(lead.error)}</div>` : ""}
    <h3>Candidates (${cands.length})</h3>${candHtml}
    ${lead.status === "review" || lead.status === "accepted" ? `<div style="margin-top:14px"><button class="mini no" id="rejectBtn">Reject lead</button></div>` : ""}`;

  $("#drawerBody").querySelectorAll("[data-choose]").forEach((b) => b.addEventListener("click", async () => {
    await fetch(`/api/leads/${id}/choose`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_index: +b.dataset.choose }) });
    closeDrawer(); refresh();
  }));
  const rej = $("#rejectBtn");
  if (rej) rej.addEventListener("click", async () => { await fetch(`/api/leads/${id}/reject`, { method: "POST" }); closeDrawer(); refresh(); });
  $("#drawer").classList.add("open");
}
function closeDrawer() { $("#drawer").classList.remove("open"); }
$("#drawerClose").addEventListener("click", closeDrawer);
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeDrawer(); });

// ── Polling ─────────────────────────────────────────────────────────────────
function startPolling() { if (!pollTimer) pollTimer = setInterval(refresh, 2000); }
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }
function refresh() { loadStats(); loadLeads(); }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

refresh();
loadStats().then((running) => { if (running) startPolling(); });
