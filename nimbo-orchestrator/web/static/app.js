"use strict";
// Nimbo Orchestrator — local web UI. Drives the same pipeline as the CLI.

const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
};

let current = null;        // selected project name
let jobPoll = null;        // interval handle while a job runs
let logPoll = null;
let MODELS = [];           // cached /api/models list

const STEPS = ["Lyrics", "Shot plan", "Draft", "Approve", "Final", "Assemble", "Done"];
const STAGE_STEP = { song: 0, lyrics: 0, plan: 1, draft: 2, approval: 3, final: 4, assemble: 5, done: 6 };
const STAGE_LABEL = (stage) => STEPS[STAGE_STEP[stage] ?? 0] || stage;

// ── boot ──────────────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  $("btn-settings").onclick = openSettings;
  $("close-settings").onclick = () => $("settings-drawer").classList.add("hidden");
  $("btn-logpanel").onclick = openLog;
  $("close-log").onclick = closeLog;
  $("np-create").onclick = createProject;
  $("save-keys").onclick = saveKeys;
  $("verbose-toggle").onchange = (e) => setVerbose(e.target.checked);
  $("log-verbose").onchange = (e) => setVerbose(e.target.checked);
  $("log-clear").onclick = async () => { await fetch("/api/log", { method: "DELETE" }); refreshLog(); };
  $("log-level").onchange = refreshLog;
  refreshHealth();
  refreshProjects();
  api("/api/models").then((m) => { MODELS = m; }).catch(() => {});
});

// ── model picker ───────────────────────────────────────────────────────────────
function modelOptions(selected) {
  // order cheapest -> priciest so "cheapest" reads top-down
  const sorted = [...MODELS].sort((a, b) => a.per_second_cost_estimate_usd - b.per_second_cost_estimate_usd);
  return sorted.map((m) => {
    const tag = m.tier === "draft" ? "cheapest" : m.tier === "pro" ? "high-end" : "mid";
    const label = `${m.label} — ~$${m.per_second_cost_estimate_usd.toFixed(3)}/s · ${tag} · up to ${m.max_seconds}s`;
    return `<option value="${m.id}" ${m.id === selected ? "selected" : ""}>${label}</option>`;
  }).join("");
}

function modelCard(p) {
  const el = div("card");
  const draft = p.project.draft_model, final = p.project.final_model;
  el.innerHTML = `<h3>🎚 Models <span class="muted" style="font-weight:400">— pick the quality tier</span></h3>
    <p class="muted">The <b>draft</b> model renders cheap previews of every shot; the <b>final</b>
      model re-renders only the shots you approve, at higher quality. Set either to any tier you like.</p>
    <label>Draft (preview) model<br><select id="sel-draft">${modelOptions(draft)}</select></label>
    <label style="margin-top:8px">Final (ship) model<br><select id="sel-final">${modelOptions(final)}</select></label>
    <div id="model-msg" class="muted" style="margin-top:6px"></div>`;
  const apply = btn("Apply models", async () => {
    const nd = el.querySelector("#sel-draft").value;
    const nf = el.querySelector("#sel-final").value;
    if (nd === draft && nf === final) { el.querySelector("#model-msg").textContent = "No change."; return; }
    await applyModels(nd, nf, draft);
  });
  el.appendChild(apply);
  return el;
}

async function applyModels(draftModel, finalModel, oldDraft, force = false) {
  try {
    const r = await api(`/api/projects/${current}/models`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_model: draftModel, final_model: finalModel, force }),
    });
    if (r.warning) alert(r.warning);
    flash("Models updated.");
    renderProject();
  } catch (e) {
    // 409 -> changing draft model resets render progress; confirm and retry forced
    if (/resets render progress/i.test(e.message)) {
      if (confirm(e.message + "\n\nRe-plan now? (your draft clips will be re-rendered)")) {
        return applyModels(draftModel, finalModel, oldDraft, true);
      }
      return;
    }
    alert("Could not change models: " + e.message);
  }
}

// ── health banner ───────────────────────────────────────────────────────────
async function refreshHealth() {
  const h = await api("/api/health");
  const issues = [];
  if (!h.ffmpeg || !h.ffprobe) issues.push("ffmpeg/ffprobe not found — rendering & assembly need it");
  if (!h.keys.FAL_KEY) issues.push("FAL_KEY not set — open Settings to add it before rendering");
  const b = $("health-banner");
  if (issues.length) { b.innerHTML = "⚠ " + issues.join(" · "); b.classList.remove("hidden"); }
  else b.classList.add("hidden");
  $("verbose-toggle").checked = h.verbose_logging;
  $("log-verbose").checked = h.verbose_logging;
}

// ── projects list ─────────────────────────────────────────────────────────────
async function refreshProjects() {
  const list = await api("/api/projects");
  const ul = $("project-list");
  ul.innerHTML = "";
  list.forEach((p) => {
    const li = document.createElement("li");
    if (p.name === current) li.classList.add("active");
    li.innerHTML = `<span>${p.name}</span><span class="pill">${STAGE_LABEL(p.stage)}</span>`;
    li.onclick = () => selectProject(p.name);
    ul.appendChild(li);
  });
}

async function createProject() {
  const name = $("np-name").value.trim();
  const topic = $("np-topic").value.trim();
  const mp3 = $("np-mp3").files[0];
  if (!name) return alert("Please enter a project name.");
  if (!topic && !mp3) return alert("Enter a topic or choose an MP3.");
  const fd = new FormData();
  fd.append("name", name);
  if (topic) fd.append("topic", topic);
  if (mp3) fd.append("mp3", mp3);
  try {
    await api(`/api/projects?name=${encodeURIComponent(name)}` + (topic ? `&topic=${encodeURIComponent(topic)}` : ""),
      { method: "POST", body: mp3 ? fd : undefined });
    $("np-name").value = $("np-topic").value = ""; $("np-mp3").value = "";
    await refreshProjects();
    selectProject(name);
  } catch (e) { alert("Create failed: " + e.message); }
}

// ── project view ──────────────────────────────────────────────────────────────
async function selectProject(name) {
  current = name;
  $("empty-state").classList.add("hidden");
  $("project-view").classList.remove("hidden");
  await refreshProjects();
  await renderProject();
}

async function renderProject() {
  if (!current) return;
  const p = await api(`/api/projects/${current}`);
  $("pv-name").textContent = current;
  // stepper
  const activeIdx = STAGE_STEP[p.stage] ?? 0;
  $("pv-stepper").innerHTML = STEPS.map((label, i) => {
    const cls = i < activeIdx ? "done" : i === activeIdx ? "active" : "";
    return `<span class="step ${cls}">${label}</span>`;
  }).join("");
  // warnings (ref images etc.)
  const w = $("pv-warnings");
  if (p.ref_warnings && p.ref_warnings.length) {
    w.innerHTML = "⚠ " + p.ref_warnings.join("<br>⚠ ");
    w.classList.remove("hidden");
  } else w.classList.add("hidden");

  const stage = $("pv-stage");
  stage.innerHTML = "";
  // model picker — available once a plan exists (it reads/writes the manifest models)
  if (p.project) stage.appendChild(modelCard(p));
  if (p.stage === "song" || p.stage === "lyrics") stage.appendChild(lyricsPanel(p));
  else if (p.stage === "plan") stage.appendChild(planPanel(p));
  else if (p.stage === "draft") stage.appendChild(draftPanel(p));
  else if (p.stage === "approval") stage.appendChild(approvalPanel(p));
  else if (p.stage === "final") stage.appendChild(finalPanel(p));
  else if (p.stage === "assemble") stage.appendChild(assemblePanel(p));
  else if (p.stage === "done") stage.appendChild(donePanel(p));

  // keep polling while a job runs
  if (p.job && p.job.status === "running") startJobPoll();
}

// ── STOP 1: lyrics ──────────────────────────────────────────────────────────
function lyricsPanel(p) {
  const el = div("card");
  el.innerHTML = `<h3>Review the lyrics</h3>
    <p class="muted">Edit any section, then continue to the shot plan.</p>`;
  const secs = (p.lyrics && p.lyrics.sections) || [];
  secs.forEach((s, i) => {
    const d = div("section-edit");
    d.innerHTML = `<label>${s.name} <span class="timing">${s.start_s.toFixed(1)}–${s.end_s.toFixed(1)}s</span></label>
      <textarea rows="4" data-i="${i}">${s.text}</textarea>`;
    el.appendChild(d);
  });
  const save = btn("💾 Save lyrics", async () => {
    const sections = secs.map((s, i) => ({ ...s, text: el.querySelector(`textarea[data-i="${i}"]`).value }));
    await api(`/api/projects/${current}/lyrics`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sections }),
    });
    flash("Saved.");
  });
  const next = btn("Looks good → build shot plan", async () => {
    await api(`/api/projects/${current}/plan`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    renderProject();
  });
  el.append(save, next);
  return el;
}

// ── STOP 2: shot plan ─────────────────────────────────────────────────────────
function planPanel(p) {
  const el = div("card");
  el.innerHTML = `<h3>Review the shot plan (${(p.shots || []).length} shots)</h3>
    <p class="muted">These cover the whole song. Continue to render low-cost drafts.</p>`;
  const t = document.createElement("table");
  t.innerHTML = `<tr><th>id</th><th>time</th><th>section→action</th><th>seed</th></tr>` +
    p.shots.map((s) => `<tr><td>${s.id}</td><td>${s.start_s.toFixed(1)}–${s.end_s.toFixed(1)}</td>
      <td>${escapeHtml(s.action)}</td><td>${s.seed_from || "ref"}</td></tr>`).join("");
  el.appendChild(t);
  el.appendChild(costAndRender(p, "draft", "🎬 Render drafts (cheapest tier)"));
  return el;
}

// ── STOP 3: draft render + approval ───────────────────────────────────────────
function draftPanel(p) {
  const el = div("card");
  el.innerHTML = `<h3>Draft render</h3><p class="muted">Renders every shot on the cheapest tier. Watch the activity log for progress.</p>`;
  el.appendChild(jobProgress(p));
  el.appendChild(costAndRender(p, "draft", "🎬 Render drafts now"));
  return el;
}

function approvalPanel(p) {
  const el = div("");
  const head = div("card");
  head.innerHTML = `<h3>Review draft clips</h3>
    <p class="muted">Approve the ones you like (they'll be re-rendered at higher quality), or mark Redo to re-draft. Then run the final pass — or assemble the drafts as-is.</p>`;
  el.appendChild(head);
  el.appendChild(shotGrid(p));
  const actions = div("card");
  actions.appendChild(costAndRender(p, "final", "✨ Final render of approved shots"));
  const asm = btn("📦 Assemble drafts as-is (skip final)", () => doAssemble());
  asm.classList.add("ghost");
  actions.appendChild(asm);
  el.appendChild(actions);
  return el;
}

function shotGrid(p) {
  const grid = div("shot-grid");
  p.shots.forEach((s) => {
    const c = div("shot-card");
    const hasClip = !!s.output_path;
    c.innerHTML = `<div><span class="badge ${s.status}">${s.status}</span> <strong>${s.id}</strong></div>
      ${hasClip ? `<video controls preload="metadata" src="/api/projects/${current}/shots/${s.id}/video"></video>` : `<p class="muted">no clip</p>`}
      <div class="muted" style="font-size:.78rem">${escapeHtml(s.action)}</div>`;
    const row = div("row");
    row.append(
      btn("✓ Approve", () => setShot(s.id, "approved")),
      btn("↻ Redo", () => setShot(s.id, "redo")),
    );
    c.appendChild(row);
    grid.appendChild(c);
  });
  return grid;
}

// ── STOP 4/5: final + assemble ────────────────────────────────────────────────
function finalPanel(p) {
  const el = div("card");
  el.innerHTML = `<h3>Final render</h3><p class="muted">Re-rendering approved shots on the locked tier.</p>`;
  el.appendChild(jobProgress(p));
  el.appendChild(costAndRender(p, "final", "✨ Final render approved shots"));
  return el;
}

function assemblePanel(p) {
  const el = div("card");
  el.innerHTML = `<h3>Assemble the final video</h3>
    <p class="muted">Concatenates the clips and muxes your MP3 as the only audio.</p>
    <label class="toggle"><input id="cap-toggle" type="checkbox"/><span>Burn in lyric captions</span></label>`;
  el.appendChild(jobProgress(p));
  el.appendChild(btn("📦 Assemble → final.mp4", () => doAssemble()));
  return el;
}

function donePanel(p) {
  const el = div("card");
  el.innerHTML = `<h3>✓ Done</h3>
    <video controls style="width:100%;border-radius:10px;background:#000" src="/api/projects/${current}/final"></video>
    <div class="row" style="margin-top:10px;display:flex;gap:8px">
      <a href="/api/projects/${current}/final" download><button>⬇ Download final.mp4</button></a>
    </div>`;
  const re = btn("Re-assemble", () => { /* jump back to assemble */ doAssemble(); });
  re.classList.add("ghost");
  el.appendChild(re);
  return el;
}

// ── cost gate + render trigger ────────────────────────────────────────────────
function costAndRender(p, which, label) {
  const wrap = div("");
  const info = div("muted");
  info.textContent = "loading cost…";
  wrap.appendChild(info);
  api(`/api/projects/${current}/cost?which=${which}`).then((c) => {
    info.innerHTML = `Estimated ${which} spend: <span class="cost">$${c.total.toFixed(2)}</span>
      <span class="muted">(${c.breakdown.length} shots on ${c.model})</span>`;
    const go = btn(label, async () => {
      if (c.total > 0 && !confirm(`This will spend about $${c.total.toFixed(2)} on the ${which} pass. Continue?`)) return;
      const r = await api(`/api/projects/${current}/render`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pass: which, confirm: true }),
      });
      if (!r.started) { alert("Nothing to render: " + (r.reason || "")); return; }
      startJobPoll();
    });
    if (c.breakdown.length === 0) { go.disabled = true; info.innerHTML += " — nothing to render"; }
    wrap.appendChild(go);
  }).catch((e) => info.textContent = "cost unavailable: " + e.message);
  return wrap;
}

async function doAssemble() {
  const cap = $("cap-toggle");
  const r = await api(`/api/projects/${current}/assemble`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ captions: cap ? cap.checked : false }),
  });
  if (r.started) startJobPoll();
}

async function setShot(id, status) {
  await api(`/api/projects/${current}/shots/${id}/status`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  renderProject();
}

// ── job progress polling ──────────────────────────────────────────────────────
function jobProgress(p) {
  const el = div("");
  el.id = "jobprog";
  const j = p.job;
  if (j && j.status === "running") {
    const pct = j.total ? Math.round((j.done + j.failed) / j.total * 100) : 0;
    el.innerHTML = `<div class="progress"><div style="width:${pct}%"></div></div>
      <div class="muted">${j.kind}: ${j.done} done, ${j.failed} failed of ${j.total}${j.current ? " · now " + j.current : ""}</div>`;
  } else if (j && j.status === "error") {
    el.innerHTML = `<div class="warnings">Job error: ${escapeHtml(j.error || "")}</div>`;
  }
  return el;
}

function startJobPoll() {
  if (jobPoll) return;
  jobPoll = setInterval(async () => {
    const j = await api(`/api/projects/${current}/job`).catch(() => null);
    if (!j || j.status !== "running") {
      clearInterval(jobPoll); jobPoll = null;
      renderProject();
    } else {
      const prog = $("jobprog");
      if (prog) {
        const pct = j.total ? Math.round((j.done + j.failed) / j.total * 100) : 0;
        prog.innerHTML = `<div class="progress"><div style="width:${pct}%"></div></div>
          <div class="muted">${j.kind}: ${j.done} done, ${j.failed} failed of ${j.total || "?"}${j.current ? " · now " + j.current : ""}</div>`;
      }
    }
    if ($("log-drawer").classList.contains("hidden") === false) refreshLog();
  }, 1500);
}

// ── settings ──────────────────────────────────────────────────────────────────
async function openSettings() {
  $("settings-drawer").classList.remove("hidden");
  const h = await api("/api/health");
  $("env-status").innerHTML = `
    <div>Python ${h.python}</div>
    <div class="${h.ffmpeg ? "ok" : "bad"}">ffmpeg ${h.ffmpeg ? "✓" : "✗ (install it)"}</div>
    <div class="${h.ffprobe ? "ok" : "bad"}">ffprobe ${h.ffprobe ? "✓" : "✗ (install it)"}</div>
    <div class="${h.keys.FAL_KEY ? "ok" : "bad"}">FAL_KEY ${h.keys.FAL_KEY ? "✓ set" : "✗ not set"}</div>
    <div class="${h.keys.GEMINI_API_KEY ? "ok" : ""}">GEMINI_API_KEY ${h.keys.GEMINI_API_KEY ? "✓ set" : "— optional"}</div>`;
  $("verbose-toggle").checked = h.verbose_logging;
}

async function saveKeys() {
  const keys = {
    FAL_KEY: $("k-fal").value.trim(), GEMINI_API_KEY: $("k-gemini").value.trim(),
    SUNO_API_KEY: $("k-suno").value.trim(), ELEVENLABS_API_KEY: $("k-eleven").value.trim(),
  };
  await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys }),
  });
  ["k-fal", "k-gemini", "k-suno", "k-eleven"].forEach((id) => $(id).value = "");
  flash("Keys saved.");
  refreshHealth(); openSettings();
}

async function setVerbose(v) {
  await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verbose_logging: v }),
  });
  $("verbose-toggle").checked = v; $("log-verbose").checked = v;
  refreshLog();
}

// ── activity log ──────────────────────────────────────────────────────────────
function openLog() {
  $("log-drawer").classList.remove("hidden");
  refreshLog();
  if (!logPoll) logPoll = setInterval(() => { if ($("log-auto").checked) refreshLog(); }, 2000);
}
function closeLog() {
  $("log-drawer").classList.add("hidden");
  if (logPoll) { clearInterval(logPoll); logPoll = null; }
}
async function refreshLog() {
  const level = $("log-level").value;
  const q = current ? `&project=${encodeURIComponent(current)}` : "";
  const data = await api(`/api/log?limit=300&level=${level}${q}`).catch(() => null);
  if (!data) return;
  const stream = $("log-stream");
  stream.innerHTML = data.events.map((e) => {
    const det = Object.keys(e.details || {}).length ? "  " + JSON.stringify(e.details) : "";
    return `<div class="log-line ${e.level}"><span class="lv">[${e.level.toUpperCase()}]</span> ${e.iso} ${e.project ? "(" + e.project + ") " : ""}${e.stage ? e.stage + ": " : ""}${escapeHtml(e.message)}${escapeHtml(det)}</div>`;
  }).join("") || `<div class="muted">no events yet</div>`;
  stream.scrollTop = stream.scrollHeight;
}

// ── tiny helpers ──────────────────────────────────────────────────────────────
function div(cls) { const d = document.createElement("div"); if (cls) d.className = cls; return d; }
function btn(label, onclick) { const b = document.createElement("button"); b.textContent = label; b.onclick = onclick; b.style.marginTop = "10px"; b.style.marginRight = "8px"; return b; }
function flash(msg) { const b = $("health-banner"); b.textContent = msg; b.classList.remove("hidden"); setTimeout(refreshHealth, 1800); }
function escapeHtml(s) { return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
