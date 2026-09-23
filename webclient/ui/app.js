// webclient UI -- a small, no-build ES-module app (roadmap N4 v0/v1/v2): a trace viewer
// (timeline · snapshots · rrweb DOM replay · plan wireframe · errors), the same view over
// the live event stream, and the loop / pipeline checkpoints with resume. It talks only to
// the service (/traces, /events, /plan, /loops) so it never touches a browser itself.
// Everything is typed with JSDoc; a TypeScript + vite build is a mechanical swap.

/** @typedef {{n?:number, topic:string, ts?:number, document_id?:string, [k:string]:any}} Ev */

const $ = (sel) => /** @type {HTMLElement} */ (document.querySelector(sel));
const api = {
  async json(path, init) {
    const r = await fetch(path, init);
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  },
  traces: () => api.json("/traces"),
  trace: (id) => api.json(`/traces/${encodeURIComponent(id)}`),
  events: (id) => api.json(`/traces/${encodeURIComponent(id)}/events`),
  rrweb: (id) => api.json(`/traces/${encodeURIComponent(id)}/rrweb`),
  asset: (id, rel) => `/traces/${encodeURIComponent(id)}/asset/${rel}`,
  loops: () => api.json("/loops"),
  resume: (id, body) => api.json(`/loops/${encodeURIComponent(id)}/resume`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  plan: (body) => api.json("/plan", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
};

const state = {
  mode: "traces",
  trace: /** @type {string|null} */ (null),
  events: /** @type {Ev[]} */ ([]),
  topics: /** @type {Set<string>} */ (new Set()),
  hidden: /** @type {Set<string>} */ (new Set()),
  selected: /** @type {Ev|null} */ (null),
  ws: /** @type {WebSocket|null} */ (null),
  cursor: 0,
  player: /** @type {any} */ (null),
};

// -- rendering -------------------------------------------------------------------
const root = (t) => t.split(".")[0];
const brief = (e) => {
  const skip = new Set(["topic", "n", "seq", "ts", "version", "source", "document_id", "session_id", "plan_id", "node_id"]);
  return Object.entries(e).filter(([k, v]) => !skip.has(k) && v !== null && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v).slice(0, 80) : String(v).slice(0, 80)}`).join(" ");
};

function renderTopics() {
  const el = $("#topics"); el.innerHTML = "";
  [...state.topics].sort().forEach((t) => {
    const l = document.createElement("label");
    const c = document.createElement("input"); c.type = "checkbox"; c.checked = !state.hidden.has(t);
    c.onchange = () => { c.checked ? state.hidden.delete(t) : state.hidden.add(t); renderEvents(); };
    l.append(c, " ", t); el.append(l);
  });
}

function renderEvents() {
  const q = $("#search").value.toLowerCase();
  const list = $("#events"); list.innerHTML = "";
  let shown = 0;
  for (const e of state.events) {
    if (state.hidden.has(root(e.topic))) continue;
    const line = `${e.topic} ${e.document_id || ""} ${brief(e)}`;
    if (q && !line.toLowerCase().includes(q)) continue;
    const li = document.createElement("li");
    if (e === state.selected) li.classList.add("active");
    li.innerHTML = `<span class="n">#${e.n ?? ""}</span><span class="topic ${root(e.topic)}">${e.topic}</span><span>${escapeHtml(brief(e))}</span>`;
    li.onclick = () => select(e);
    list.append(li); shown++;
  }
  $("#count").textContent = `${shown} / ${state.events.length}`;
  if ($("#follow").checked) list.lastElementChild?.scrollIntoView({ block: "end" });
}

const escapeHtml = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function select(e) {
  state.selected = e;
  $("#event-json").textContent = JSON.stringify(e, null, 2);
  renderEvents();
  if (e.topic === "snapshot") { showTab("snapshot"); showSnapshot(e); }
  else if (e.topic === "rrweb") { showTab("replay"); showReplay(e.document_id); }
  else if (root(e.topic) === "error") showTab("errors");
  else showTab("event");
}

function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.dataset.tab !== name));
}

async function showSnapshot(e) {
  const meta = $("#snapshot-meta");
  meta.textContent = `${e.phase} · ${e.kind} · ${e.status_code} · ${e.final_url || e.url} · tiers ${JSON.stringify(e.tiers || [])}`;
  const frame = /** @type {HTMLIFrameElement} */ ($("#snapshot"));
  if (state.trace && e.asset) {
    const r = await fetch(api.asset(state.trace, e.asset));
    frame.srcdoc = e.kind === "html" ? await r.text() : `<pre>${escapeHtml(await r.text())}</pre>`;
  } else if (e.content) {
    frame.srcdoc = String(e.content);
  } else {
    frame.srcdoc = "<p>The snapshot's content is not streamed live; open the trace to see it.</p>";
  }
}

async function showReplay(documentId) {
  const holder = $("#player"); holder.innerHTML = "";
  const note = $("#replay-note");
  if (!state.trace) { note.textContent = "DOM replay reads a stored trace (rrweb chunks are not streamed live)."; return; }
  const events = await api.rrweb(state.trace);
  const mine = documentId ? events.filter((x) => true) : events;  // chunks are flattened per trace
  if (!mine.length) { note.textContent = "No rrweb recording in this trace (rrweb records only under wc.trace())."; return; }
  note.textContent = `${mine.length} rrweb events`;
  // @ts-ignore -- the vendored UMD global
  state.player = new rrwebPlayer({ target: holder, props: { events: mine, width: holder.clientWidth || 800, height: 420, autoPlay: false } });
}

function renderErrors() {
  const ul = $("#errors"); ul.innerHTML = "";
  for (const e of state.events.filter((x) => root(x.topic) === "error")) {
    const err = e.error || {};
    const li = document.createElement("li");
    li.innerHTML = `<code>${escapeHtml(err.code || err.type || "?")}</code> ${e.raised ? "raised" : "recorded"} · <b>${escapeHtml(err.op || "")}</b> ${escapeHtml(err.subject || "")}<br>` +
      `${escapeHtml(err.message || "")}<br><span class="muted">remedy: ${escapeHtml(err.remedy || "-")} — ${escapeHtml(err.hint || "")}</span>`;
    ul.append(li);
  }
}

async function renderLoops() {
  const el = $("#loops"); el.innerHTML = "";
  const seen = new Map();
  for (const e of state.events) {
    if (root(e.topic) === "loop") seen.set(`loop:${e.loop}`, { kind: "loop", name: e.loop, phase: e.phase, round: e.round, detail: e.detail || {} });
    if (root(e.topic) === "pipeline") seen.set(`pipeline:${e.pipeline}`, { kind: "pipeline", name: e.pipeline, phase: `${e.stage}:${e.phase}`, detail: e.detail || {} });
  }
  let waiting = [];
  if (state.mode === "live") { try { waiting = await api.loops(); } catch { waiting = []; } }
  for (const [, l] of seen) {
    const d = document.createElement("div"); d.className = "loop";
    d.innerHTML = `<b>${escapeHtml(l.name)}</b> <span class="muted">${escapeHtml(l.kind)} · ${escapeHtml(String(l.phase))}${l.round ? " · round " + l.round : ""}</span>`;
    el.append(d);
  }
  for (const w of waiting) {
    const d = document.createElement("div"); d.className = "ask";
    d.innerHTML = `<b>${escapeHtml(w.id)}</b> <span class="muted">${escapeHtml(w.kind)}</span><br>${escapeHtml(w.ask?.reason || "")}<br>`;
    const opts = (w.ask?.options || []).slice(0, 12);
    for (const o of opts) {
      const b = document.createElement("button"); b.textContent = String(o);
      b.onclick = async () => { await api.resume(w.id, { answer: o }); renderLoops(); };
      d.append(b);
    }
    const inp = document.createElement("input"); inp.placeholder = "custom answer";
    const go = document.createElement("button"); go.textContent = "resume";
    go.onclick = async () => { await api.resume(w.id, { answer: inp.value }); renderLoops(); };
    d.append(inp, go);
    el.append(d);
  }
}

// -- sources: a stored trace, or the live stream ---------------------------------------
async function loadTraces() {
  const ul = $("#traces"); ul.innerHTML = "";
  let traces = [];
  try { traces = await api.traces(); } catch (e) { $("#status").textContent = "no /traces endpoint"; return; }
  for (const t of traces) {
    const li = document.createElement("li");
    li.textContent = `${t.id} · ${t.events} ev`;
    li.classList.toggle("active", t.id === state.trace);
    li.onclick = () => openTrace(t.id);
    ul.append(li);
  }
}

function ingest(events, { reset }) {
  if (reset) { state.events = []; state.topics = new Set(); }
  for (const e of events) { state.events.push(e); state.topics.add(root(e.topic)); if (e.n) state.cursor = Math.max(state.cursor, e.n); }
  renderTopics(); renderEvents(); renderErrors(); renderLoops();
}

async function openTrace(id) {
  closeLive();
  state.trace = id; state.mode = "traces";
  $("#status").textContent = `trace ${id}`;
  ingest(await api.events(id), { reset: true });
  loadTraces();
}

function openLive() {
  state.trace = null; state.mode = "live";
  ingest([], { reset: true });
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/events?since=${state.cursor}`);
  state.ws = ws;
  ws.onopen = () => { $("#status").textContent = "live"; };
  ws.onmessage = (m) => ingest([JSON.parse(m.data)], { reset: false });
  ws.onclose = () => { $("#status").textContent = "live: disconnected"; };
}

function closeLive() { if (state.ws) { state.ws.close(); state.ws = null; } }

// -- plan wireframe ------------------------------------------------------------------
async function renderPlan() {
  const raw = $("#plan-blob").value.trim();
  if (!raw) return;
  const body = raw.startsWith("{") ? { plan: JSON.parse(raw), wireframe: true } : { blob: raw, wireframe: true };
  try {
    const out = await api.plan(body);
    $("#plan-describe").textContent = out.describe || "";
    /** @type {HTMLIFrameElement} */ ($("#wireframe")).srcdoc = out.wireframe || "";
  } catch (e) { $("#plan-describe").textContent = String(e); }
}

// -- wiring -------------------------------------------------------------------------
document.querySelectorAll("header nav button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("header nav button").forEach((x) => x.classList.toggle("active", x === b));
  b.dataset.mode === "live" ? openLive() : (closeLive(), loadTraces());
}));
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("#search").addEventListener("input", renderEvents);
$("#refresh").addEventListener("click", loadTraces);
$("#plan-render").addEventListener("click", renderPlan);
loadTraces();
