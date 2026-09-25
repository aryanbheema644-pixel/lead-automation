const $ = (sel) => document.querySelector(sel);
let currentFilter = "all";
let pollTimer = null;

// Parse a response safely: return JSON when possible, otherwise surface the
// raw text (e.g. a 500 "Internal Server Error") as a readable message.
async function readResponse(r) {
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); }
  catch { data = { detail: text.slice(0, 300) || `HTTP ${r.status}` }; }
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// ── Upload ────────────────────────────────────────────────────────────────
const fileInput = $("#fileInput");
const filedrop = $("#filedrop");

filedrop.addEventListener("dragover", (e) => { e.preventDefault(); filedrop.classList.add("drag"); });
filedrop.addEventListener("dragleave", () => filedrop.classList.remove("drag"));
filedrop.addEventListener("drop", (e) => {
  e.preventDefault(); filedrop.classList.remove("drag");
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) uploadFile(fileInput.files[0]);
});

async function uploadFile(file) {
  const msg = $("#uploadMsg");
  msg.className = "msg"; msg.textContent = `Uploading ${file.name}…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await readResponse(r);
    msg.className = "msg ok";
    msg.textContent = `Added ${data.inserted} leads. Mapped: ${Object.keys(data.mapped_columns).join(", ")}. Click “Run pipeline”.`;
    refresh();
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
}

// ── Run / reset ─────────────────────────────────────────────────────────────
$("#runBtn").addEventListener("click", async () => {
  const btn = $("#runBtn");
  btn.disabled = true;
  try {
    const r = await fetch("/api/run", { method: "POST" });
    const data = await readResponse(r);
    $("#uploadMsg").className = "msg ok";
    $("#uploadMsg").textContent = `Processing ${data.queued} queued leads…`;
    startPolling();
  } catch (e) {
    $("#uploadMsg").className = "msg err";
    $("#uploadMsg").textContent = e.message;
  } finally {
    setTimeout(() => (btn.disabled = false), 1500);
  }
});

$("#resetBtn").addEventListener("click", async () => {
  if (!confirm("Delete all leads and results?")) return;
  await fetch("/api/reset", { method: "POST" });
  refresh();
});

// ── Filters ─────────────────────────────────────────────────────────────────
$("#filters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip");
  if (!btn) return;
  document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
  btn.classList.add("active");
  currentFilter = btn.dataset.status;
  loadLeads();
});

// ── Rendering ────────────────────────────────────────────────────────────────
function badge(status, stage) {
  const label = status === "processing" && stage ? stage : status;
  const spin = status === "processing" ? '<span class="spin">◠</span>' : "";
  return `<span class="badge ${status}">${spin}${label}</span>`;
}

function confBar(score) {
  if (score == null) return "—";
  const pct = Math.round(score * 100);
  const color = score >= 0.75 ? "var(--green)" : score >= 0.45 ? "var(--amber)" : "var(--red)";
  return `<span class="conf-bar"><span class="conf-fill" style="width:${pct}%;background:${color}"></span></span><span class="conf-num">${score.toFixed(2)}</span>`;
}

const SOURCE_LABELS = {
  linkedin: "LinkedIn profile",
  scholar: "Google Scholar / ResearchGate",
  github: "GitHub",
  company: "Company page",
  imdb: "IMDb",
  social: "Social profile",
  news: "News / article",
  pdf: "PDF document",
  personal_or_other: "Personal site / other",
};

function prettyUrl(url) {
  let u = String(url || "").replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/$/, "");
  return u.length > 44 ? u.slice(0, 44) + "…" : u;
}

function topMatch(lead) {
  const c = (lead.candidates || [])[0];
  if (!c) return '<span class="sub">—</span>';
  const label = SOURCE_LABELS[c.source_type] || c.source_type;
  return `<div class="tm-label">${esc(label)}</div>`
       + `<a class="tm-url" href="${c.url}" target="_blank" rel="noopener">${esc(prettyUrl(c.url))}</a>`;
}

async function loadLeads() {
  const r = await fetch(`/api/leads?status=${currentFilter}`);
  const { leads } = await r.json();
  const tbody = $("#leadRows");
  if (!leads.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">No leads in this view.</td></tr>`;
    return;
  }
  tbody.innerHTML = leads.map((l) => `
    <tr data-id="${l.id}">
      <td class="sub">${l.id}</td>
      <td class="name-cell">${esc(l.name || "—")}<div class="sub">${esc(l.email || "")}</div></td>
      <td>${esc(l.company || "—")}</td>
      <td>${badge(l.status, l.stage)}</td>
      <td>${topMatch(l)}</td>
      <td>${rowActions(l)}</td>
    </tr>`).join("");
  tbody.querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", () => openDrawer(b.dataset.open)));
  tbody.querySelectorAll("[data-accept]").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); acceptTop(b.dataset.accept); }));
  tbody.querySelectorAll("[data-reject]").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); rejectLead(b.dataset.reject); }));
}

function rowActions(l) {
  let btns = "";
  if (l.status === "review") {
    btns += `<button class="mini ok" data-accept="${l.id}">Accept</button>`
          + `<button class="mini no" data-reject="${l.id}">Reject</button>`;
  } else if (l.status === "accepted") {
    btns += `<button class="mini no" data-reject="${l.id}">Reject</button>`;
  }
  btns += `<button class="link-btn" data-open="${l.id}">Details ▸</button>`;
  return `<div class="row-actions">${btns}</div>`;
}

// Accept the highest-scored candidate (top of the list) directly from the row.
async function acceptTop(id) {
  await fetch(`/api/leads/${id}/choose`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ candidate_index: 0 }),
  });
  refresh();
}
async function rejectLead(id) {
  await fetch(`/api/leads/${id}/reject`, { method: "POST" });
  refresh();
}

async function loadStats() {
  const r = await fetch("/api/status");
  const { stats, running, providers } = await r.json();
  const cells = [
    ["total", "Total"], ["queued", "Queued"], ["processing", "Processing"],
    ["accepted", "Accepted"], ["review", "Review"], ["rejected", "Rejected"],
  ];
  $("#stats").innerHTML = cells.map(([k, lbl]) =>
    `<div class="stat ${k}"><div class="num">${stats[k] || 0}</div><div class="lbl">${lbl}</div></div>`).join("");

  const p = providers;
  const prov = [
    ["OpenRouter", p.openrouter, p.model],
    ["Tavily", p.tavily], ["Firecrawl", p.firecrawl], ["Apify", p.apify],
  ];
  $("#providers").innerHTML = prov.map(([name, on, extra]) =>
    `<span class="pill ${on ? "on" : ""}" title="${extra || ""}"><span class="dot"></span>${name}</span>`).join("");

  if (!running && (stats.queued === 0 && stats.processing === 0)) stopPolling();
  return running;
}

// ── Detail drawer ────────────────────────────────────────────────────────────
async function openDrawer(id) {
  const r = await fetch(`/api/leads/${id}`);
  const lead = await r.json();
  const ex = lead.extracted || {};
  const cands = lead.candidates || [];

  const candHtml = cands.map((c, i) => {
    const sig = c.signals || {};
    const sigTag = (label, v) => `<span class="sig ${v ? "yes" : "no"}">${label} ${v ? "✓" : "✕"}</span>`;
    const reviewable = lead.status === "review" || lead.status === "accepted";
    return `
      <div class="cand ${i === 0 ? "best" : ""}">
        <div class="cand-head">
          <span class="cand-type">${c.source_type} · <span class="src-tag">via ${c.content_source}</span></span>
          <span class="cand-score">${(c.score ?? 0).toFixed(2)}</span>
        </div>
        <b>${esc(c.person?.name || c.title || "")}</b>
        <a class="cand-url" href="${c.url}" target="_blank" rel="noopener">${esc(c.url)}</a>
        <div class="signals">
          ${sigTag("name", sig.name)}${sigTag("company", sig.company)}
          ${sigTag("school", sig.school)}${sigTag("location", sig.location)}${sigTag("role", sig.role)}
        </div>
        <div class="cand-reason">${esc(c.reasoning || "")}</div>
        ${reviewable ? `<div class="cand-actions"><button class="btn primary" data-choose="${i}">Select this match</button></div>` : ""}
      </div>`;
  }).join("") || '<p class="sub">No candidates.</p>';

  $("#drawerBody").innerHTML = `
    <h2>${esc(lead.name || "Lead #" + lead.id)}</h2>
    <div class="sub">${badge(lead.status, lead.stage)} ${lead.confidence != null ? confBar(lead.confidence) : ""}</div>

    <h3>Raw input</h3>
    <div class="kv">
      <span class="k">Company</span><span>${esc(lead.company || "—")}</span>
      <span class="k">Email</span><span>${esc(lead.email || "—")}</span>
      <span class="k">Phone</span><span>${esc(lead.phone || "—")}</span>
      <span class="k">Message</span><span>${esc(lead.message || "—")}</span>
    </div>

    <h3>Extracted &amp; enriched</h3>
    <div class="kv">
      <span class="k">Location</span><span>${esc(ex.location || "—")}</span>
      <span class="k">Role guess</span><span>${esc(ex.role_guess || "—")}</span>
      <span class="k">Notes</span><span>${esc(ex.notes || "—")}</span>
    </div>
    <div class="tags" style="margin-top:8px">${(ex.keywords || []).map(k => `<span class="tag">${esc(k)}</span>`).join("")}</div>

    ${lead.queries ? `<h3>Search queries</h3><div class="tags">${lead.queries.map(q => `<span class="tag">${esc(q)}</span>`).join("")}</div>` : ""}

    ${lead.error ? `<h3>Error</h3><div class="cand-reason" style="color:var(--red)">${esc(lead.error)}</div>` : ""}

    <h3>Candidates (${cands.length})</h3>
    ${candHtml}

    ${lead.status === "review" || lead.status === "accepted" ? `<div style="margin-top:14px"><button class="btn danger-ghost" id="rejectBtn">Reject lead</button></div>` : ""}
  `;

  $("#drawerBody").querySelectorAll("[data-choose]").forEach((b) =>
    b.addEventListener("click", async () => {
      await fetch(`/api/leads/${id}/choose`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_index: +b.dataset.choose }),
      });
      closeDrawer(); refresh();
    }));
  const rej = $("#rejectBtn");
  if (rej) rej.addEventListener("click", async () => {
    await fetch(`/api/leads/${id}/reject`, { method: "POST" });
    closeDrawer(); refresh();
  });

  $("#drawer").classList.add("open");
}

function closeDrawer() { $("#drawer").classList.remove("open"); }
$("#drawerClose").addEventListener("click", closeDrawer);
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeDrawer(); });

// ── Polling ──────────────────────────────────────────────────────────────────
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refresh, 2500);
}
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

function refresh() { loadStats(); loadLeads(); }

// ── Utils ────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Boot
refresh();
loadStats().then((running) => { if (running) startPolling(); });
