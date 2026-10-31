"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { meta: null, recipes: [], schedules: [], hosts: [], weights: [], busy: new Set(), tab: "recipes", blur: true, schedPaused: false };
try { state.blur = localStorage.getItem("llm.blur") !== "0"; } catch { /* storage unavailable */ }
const STATUS_LABEL = { ready: "Ready", loading: "Loading model", starting: "Starting", stopped: "Stopped", failed: "Failed", offline: "Host offline" };
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 204) return null;
  const text = await res.text();
  let data = text;
  try { data = JSON.parse(text); } catch { /* plain text */ }
  if (!res.ok) {
    let msg = data?.detail ?? data ?? res.statusText;
    if (Array.isArray(msg)) msg = msg.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("\n");
    throw new Error(msg);
  }
  return data;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 9000 : 4500);
}

function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/* ---------------- tabs ---------------- */

$$(".tab").forEach((btn) => btn.addEventListener("click", () => {
  state.tab = btn.dataset.tab;
  $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${state.tab}`));
  if (state.tab === "schedules") loadSchedules();
  if (state.tab === "activity") loadEvents();
  if (state.tab === "hosts") renderHosts();
  if (state.tab === "images") loadImages();
  if (state.tab === "models") { loadWeights(); loadModels(); }
}));

$$("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

/* ---------------- hosts ---------------- */

const multiHost = () => state.hosts.length > 1;
const hostById = (id) => state.hosts.find((h) => h.id === Number(id));
const hostName = (id) => hostById(id)?.name ?? `#${id}`;
const hostPlatform = (id) => hostById(id)?.info?.platform ?? "";
const PLATFORM = { darwin: "macOS", linux: "Linux" };

function memText(m) { return `${m.used_gb} / ${m.total_gb} GB`; }

/* Blur a host name / IP when the "Blur" toggle in the top bar is on, for screenshots and screen
 * shares. Nothing of the real name is shown: each name becomes a stable alias such as "host-3f2a"
 * (an IPv4 address "ip-…"), so different machines still look different. Only display strings are
 * masked; the underlying values stay untouched. */
function aliasHash(s) {
  let h = 0x811c9dc5;  // FNV-1a
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return (h >>> 0).toString(16).padStart(8, "0").slice(0, 4);
}
function maskAddr(name) {
  const s = String(name ?? "").trim();
  if (!s) return "–";
  if (!state.blur) return s;
  if (s === "localhost" || s === "127.0.0.1" || s === "::1") return s;
  return `${/^(?:\d{1,3}\.){3}\d{1,3}$/.test(s) ? "ip" : "host"}-${aliasHash(s.toLowerCase())}`;
}
function maskUrl(url) {
  if (!url) return "–";
  if (!state.blur) return url;
  try {
    const u = new URL(url);
    const host = maskAddr(u.hostname);
    return u.port ? `${host}:${u.port}` : host;
  } catch {
    return maskAddr(url);
  }
}
// Full endpoint URL (http://host:port/v1) with only the host part masked, so it stays recognisable.
function maskEndpoint(urlStr) {
  if (!state.blur) return urlStr;
  try {
    const u = new URL(urlStr);
    return urlStr.replace(`//${u.hostname}`, `//${maskAddr(u.hostname)}`);
  } catch {
    return maskAddr(urlStr);
  }
}
// Mask a host's own name (from its URL) and any IPv4 address inside free text such as an error message.
function blurHostText(text, url) {
  let str = String(text ?? "");
  if (!state.blur) return str;
  try { const h = new URL(url).hostname; if (h) str = str.split(h).join(maskAddr(h)); } catch { /* no URL */ }
  return blurText(str);
}
// Mask bare IPv4 addresses inside free text (e.g. activity log messages).
function blurText(s) {
  const str = String(s ?? "");
  if (!state.blur) return str;
  return str.replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, (m) => maskAddr(m));
}
function memPct(m) { return m.total_gb ? (m.used_gb / m.total_gb) * 100 : 0; }
function barClass(pct) { return "bar-fill" + (pct > 90 ? " crit" : pct > 75 ? " warn" : ""); }

// Address of a model endpoint: the host's own name for remote hosts, else the one in the address bar.
function endpointHost(hostId) {
  const h = hostById(hostId);
  if (h?.url) { try { return new URL(h.url).hostname; } catch { /* fall through */ } }
  return location.hostname;
}

async function loadHosts() {
  try {
    [state.hosts, state.joins] = await Promise.all([api("/api/hosts"), api("/api/join-requests").catch(() => state.joins ?? [])]);
  } catch { return; }
  renderHostsStrip();
  renderHostOptions();
  renderJoinRequests();
  if (state.tab === "hosts") renderHosts();
}

/* Machines asking to be added (DGXM_HUB_URL on that machine): approve only if the codes match. */
function renderJoinRequests() {
  const pending = (state.joins || []).filter((r) => r.state === "pending");
  const rejected = (state.joins || []).filter((r) => r.state === "rejected");
  $("#join-count").textContent = pending.length || "";
  $("#join-count").classList.toggle("hidden", !pending.length);
  const box = $("#join-requests");
  box.classList.toggle("hidden", !pending.length && !rejected.length);
  if (!pending.length && !rejected.length) { box.innerHTML = ""; return; }
  const info = (r) => [r.info?.platform && (PLATFORM[r.info.platform] ?? r.info.platform), r.info?.machine, r.info?.version && `v${r.info.version}`].filter(Boolean).join(" · ");
  box.innerHTML = `<h3>🔔 Asking to be added</h3>
    <p class="hint">Approve a machine only if <b>it shows the same code</b> – in its installer output, its log
      (<code>journalctl -u local-llm-manager</code>) or its start page. Another machine using the same name has another code.</p>
    ${pending.map((r) => `<div class="join-row" data-join="${r.id}">
      <div><b>${esc(maskAddr(r.name))}</b> ${transportBadge(r)}<br><small class="muted">${esc(maskUrl(r.url))} · from ${esc(blurText(r.ip))}${info(r) ? ` · ${esc(info(r))}` : ""}<br>asked ${esc(fmtTime(r.created_at))}</small></div>
      <div class="join-code mono" title="Compare with the code the machine shows">${esc(r.code)}</div>
      <div class="join-actions"><button class="btn small primary" data-join-act="approve">Codes match – add</button>
        <button class="btn small ghost danger" data-join-act="reject">Reject</button></div></div>`).join("")}
    ${rejected.length ? `<small class="muted">Rejected (blocked for a week): ${rejected.map((r) => `${esc(maskAddr(r.name))} <span class="mono">${esc(r.code)}</span>`).join(", ")}</small>` : ""}`;
}

$("#join-requests").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-join-act]");
  if (!b) return;
  const id = Number(b.closest("[data-join]").dataset.join);
  const r = state.joins.find((x) => x.id === id);
  try {
    if (b.dataset.joinAct === "approve") {
      if (!confirm(`Add “${r.name}” (${r.url})?\n\nOnly if the machine shows the code ${r.code}.`)) return;
      let res;
      try {
        res = await api(`/api/join-requests/${id}/approve`, { method: "POST", body: {} });
      } catch (e) {
        if (!/exists already/.test(e.message)) throw e;
        const name = prompt(`${e.message}\n\nName for this machine:`, `${r.name}-2`);
        if (!name) return;
        res = await api(`/api/join-requests/${id}/approve`, { method: "POST", body: { name } });
      }
      toast(`${res.name} added${res.online ? "" : " – not reachable yet at its address"}`, res.online ? "ok" : "bad");
    } else {
      if (!confirm(`Reject “${r.name}” (code ${r.code})? It is blocked for a week.`)) return;
      await api(`/api/join-requests/${id}/reject`, { method: "POST" });
    }
    loadHosts();
  } catch (e) { toast(e.message, "bad"); }
});

function renderHostsStrip() {
  $("#hosts-strip").innerHTML = state.hosts.map((h) => {
    if (!h.online) {
      return `<button class="host-chip offline" data-host="${h.id}" title="${esc(blurHostText(h.error || "offline", h.url))}">
        <span class="host-name">${esc(maskAddr(h.name))}</span><span class="host-mem">offline</span></button>`;
    }
    const m = h.system.memory;
    const pct = memPct(m);
    const gpu = h.system.gpus[0];
    const title = `${maskAddr(h.name)}: ${memText(m)} used, ${m.available_gb} GB free` + (gpu ? `\nGPU ${gpu.utilization ?? "?"}% · ${gpu.temperature ?? "?"}°C · ${gpu.power_w ?? "?"} W` : "");
    return `<button class="host-chip" data-host="${h.id}" title="${esc(title)}">
        <span class="host-name">${esc(maskAddr(h.name))} ${h.is_local ? '<span class="host-note">this machine</span>' : '<span class="host-note">remote</span>'}</span>
        <span class="bar"><span class="${barClass(pct)}" style="width:${pct.toFixed(1)}%"></span></span>
        <span class="host-mem">${esc(memText(m))}${gpu ? ` · GPU ${gpu.utilization ?? "?"}%` : ""}</span>
      </button>`;
  }).join("");
}

$("#hosts-strip").addEventListener("click", () => $('.tab[data-tab="hosts"]').click());

/* ---------------- blur toggle (top bar) ---------------- */

function renderBlurToggle() {
  const b = $("#btn-blur");
  b.classList.toggle("on", state.blur);
  b.setAttribute("aria-pressed", String(state.blur));
  b.textContent = state.blur ? "🙈 Blur" : "👁 Show";
  b.title = state.blur ? "IP addresses and host names are blurred – click to show them"
    : "IP addresses and host names are visible – click to blur them";
}

$("#btn-blur").addEventListener("click", () => {
  state.blur = !state.blur;
  try { localStorage.setItem("llm.blur", state.blur ? "1" : "0"); } catch { /* ignore */ }
  renderBlurToggle();
  renderHostsStrip();
  renderHostOptions();
  renderRecipes();
  if (state.tab === "hosts") renderHosts();
  if (state.tab === "schedules") loadSchedules();
  if (state.tab === "activity") loadEvents();
  if (state.tab === "images") renderImages();
  if (state.tab === "models") { renderWeights(); renderModels(); }
});
renderBlurToggle();

/* ---------------- theme toggle (top bar) ---------------- */

try { state.theme = ["light", "auto"].includes(localStorage.getItem("llm.theme")) ? localStorage.getItem("llm.theme") : "dark"; } catch { state.theme = "dark"; }

function effectiveTheme() {
  return state.theme === "auto" ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : state.theme;
}
function applyTheme() {
  document.documentElement.dataset.theme = effectiveTheme();
}
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (state.theme === "auto") applyTheme(); });

function renderThemeToggle() {
  const b = $("#btn-theme");
  b.textContent = { dark: "🌙 Dark", light: "☀️ Light", auto: "🌗 Auto" }[state.theme];
  b.title = { dark: "Dark theme – click for light", light: "Light theme – click to follow the system", auto: `Following the system (currently ${effectiveTheme()}) – click for dark` }[state.theme];
}

$("#btn-theme").addEventListener("click", () => {
  state.theme = { dark: "light", light: "auto", auto: "dark" }[state.theme];
  try { localStorage.setItem("llm.theme", state.theme); } catch { /* ignore */ }
  applyTheme();
  renderThemeToggle();
});
renderThemeToggle();

function renderHostOptions() {
  const opts = state.hosts.map((h) => `<option value="${h.id}">${esc(maskAddr(h.name))}${h.is_local ? " (this machine)" : ""}</option>`).join("");
  const filter = $("#recipe-host");
  const cur = filter.value;
  filter.innerHTML = `<option value="">All hosts</option>` + opts;
  filter.value = state.hosts.some((h) => String(h.id) === cur) ? cur : "";
  filter.closest("label").classList.toggle("hidden", !multiHost());
  // Editor selects are refreshed when their dialog opens, so an open form keeps its choice.
}

// Hub ↔ agent traffic (and copies between machines) is only encrypted over Tailscale or HTTPS.
const TRANSPORT = {
  tailscale: ["ok", "🔒 Tailscale", "Encrypted by Tailscale (WireGuard)"],
  https: ["ok", "🔒 HTTPS", "Encrypted by HTTPS"],
  plain: ["warn", "⚠ plain HTTP", "Not encrypted: the agent token, commands and copied weights travel readable on the network. Use the host's Tailscale name (or HTTPS) as its address."],
};
function transportBadge(h) {
  const t = TRANSPORT[h.transport];
  return t ? `<span class="chip small ${t[0] === "ok" ? "secret ok" : "warn-chip"}" title="${esc(t[2])}">${t[1]}</span>` : "";
}
const plainHosts = (ids) => state.hosts.filter((h) => ids.includes(h.id) && h.transport === "plain");

function renderHosts() {
  renderPorts();
  const running = (id) => state.recipes.filter((r) => r.host_id === id && RUNNING.includes(r.status)).length;
  const total = (id) => state.recipes.filter((r) => r.host_id === id).length;
  $("#hosts").innerHTML = state.hosts.map((h) => {
    const m = h.system?.memory;
    const gpu = h.system?.gpus?.[0];
    return `<tr data-id="${h.id}">
      <td><b>${esc(maskAddr(h.name))}</b>${h.is_local ? ` <span class="chip small on">this machine</span>` : `<br><small class="host-url">${esc(maskUrl(h.url))}</small> ${transportBadge(h)}`}</td>
      <td>${h.online ? `<span class="ok">● online</span>` : `<span class="bad">● offline</span><br><small>${esc(blurHostText(h.error || "", h.url))}</small>`}</td>
      <td>${h.info ? `${esc(PLATFORM[h.info.platform] ?? h.info.platform)} · ${esc(h.info.machine)}<br><small>${esc(maskAddr(h.info.hostname))} · v${esc(h.info.version)}</small>` : "–"}</td>
      <td style="min-width:160px">${m ? `<div class="bar"><div class="${barClass(memPct(m))}" style="width:${memPct(m).toFixed(1)}%"></div></div>
        <small>${esc(memText(m))} · ${m.available_gb} GB free</small>` : "–"}</td>
      <td>${gpu ? `${gpu.utilization ?? "?"}% · ${gpu.temperature ?? "?"}°C` : "–"}</td>
      <td>${running(h.id)} running / ${total(h.id)}</td>
      <td style="white-space:nowrap">
        <button class="btn small ghost" data-act="stop-all" ${h.online ? "" : "disabled"}>Stop all</button>
        <button class="btn small ghost" data-act="free" ${h.online ? "" : "disabled"} title="Stop all models on this host and drop the page cache">Free memory</button>
        <button class="btn small ghost" data-act="edit">Edit</button>
        ${h.is_local ? "" : `<button class="btn small ghost danger" data-act="delete">Remove</button>`}
      </td></tr>`;
  }).join("");
}

$("#hosts").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const h = hostById(btn.closest("tr").dataset.id);
  const act = btn.dataset.act;
  if (act === "stop-all") {
    if (confirm(`Stop all models on ${h.name}?`)) await runAction(`host-${h.id}`, `/api/system/stop-all?host_id=${h.id}`);
  } else if (act === "free") {
    if (confirm(`Stop all models on ${h.name} and drop its page cache?`)) await runAction(`host-${h.id}`, `/api/system/free-memory?host_id=${h.id}`);
  } else if (act === "edit") {
    openHostEditor(h);
    return;
  } else if (act === "delete") {
    if (!confirm(`Remove host ${h.name} from this dashboard? (Nothing is changed on the machine.)`)) return;
    try { await api(`/api/hosts/${h.id}`, { method: "DELETE" }); toast("Host removed", "ok"); } catch (e) { toast(e.message, "bad"); }
  }
  loadHosts();
});

const hostForm = $("#form-host");
let editingHost = null;

function openHostEditor(h) {
  editingHost = h;
  hostForm.reset();
  $("#host-error").textContent = "";
  $("#host-title").textContent = h ? `Edit host – ${h.name}` : "Add host";
  hostForm.elements.name.value = h?.name ?? "";
  hostForm.elements.url.value = h?.url ?? "";
  const local = h?.is_local;
  $("#lbl-host-url").classList.toggle("hidden", !!local);
  $("#lbl-host-token").classList.toggle("hidden", !!local);
  $("#host-token-hint").textContent = h ? "leave empty to keep the current token" : "the agent's DGXM_AGENT_TOKEN";
  $("#dlg-host").showModal();
}

$("#btn-new-host").addEventListener("click", () => openHostEditor(null));

hostForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = { name: hostForm.elements.name.value, url: hostForm.elements.url.value, token: hostForm.elements.token.value };
  try {
    const h = editingHost
      ? await api(`/api/hosts/${editingHost.id}`, { method: "PUT", body })
      : await api("/api/hosts", { method: "POST", body });
    $("#dlg-host").close();
    toast(h.online ? `${h.name} is online` : `Saved, but ${h.name} is not reachable: ${h.error}`, h.online ? "ok" : "bad");
    await loadHosts();
    loadRecipes();
  } catch (e) {
    $("#host-error").textContent = e.message;
  }
});

/* ---------------- recipes ---------------- */

async function loadRecipes() {
  try {
    state.recipes = await api("/api/recipes");
    recordMetrics(state.recipes);
    if (watchStarts(state.recipes)) await loadStartTimes();  // a boot just finished
    renderRecipes();
  } catch (e) {
    $("#recipes").innerHTML = `<div class="empty">Failed to load recipes: ${esc(e.message)}</div>`;
  }
}

/* ---------------- recipe list: filter, sort, group ---------------- */

const VIEW_KEY = "dgxm.recipeView";
const view = { search: "", tags: [], sort: "name", group: "", host: "", kind: "", layout: "rows", collapsed: {} };
try { Object.assign(view, JSON.parse(localStorage.getItem(VIEW_KEY) || "{}")); } catch { /* storage unavailable */ }
if (typeof view.group === "boolean") view.group = view.group ? "tag" : "";  // older saved views
const saveView = () => { try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch { /* ignore */ } };

const RUNNING = ["ready", "loading", "starting"];
const STATUS_ORDER = { ready: 0, loading: 1, starting: 2, failed: 3, stopped: 4, offline: 5 };

/* Start-duration history (server-measured trigger→ready) and a live timer while a model boots. */
state.startTimes = {};                       // recipe id -> [seconds, newest first]
const startWatch = new Map();                // recipe id -> ts of first "starting" observation
async function loadStartTimes() { try { state.startTimes = await api("/api/start-times"); } catch { /* keep last */ } }
function watchStarts(recipes) {
  const now = Date.now();
  let finished = false;
  for (const r of recipes) {
    const booting = r.status === "starting" || r.status === "loading";
    if (booting && !startWatch.has(r.id)) startWatch.set(r.id, now);
    if (!booting && startWatch.has(r.id)) { startWatch.delete(r.id); finished = true; }
  }
  return finished;
}
const median = (a) => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
function fmtDur(s) {
  s = Math.round(s);
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${Math.floor(s / 3600)}h ${Math.round(s % 3600 / 60)}m`;
}
const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
const SORTS = {
  name: byName,
  status: (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || byName(a, b),
  framework: (a, b) => a.framework.localeCompare(b.framework) || byName(a, b),
  port: (a, b) => a.host_port - b.host_port || byName(a, b),
  updated: (a, b) => (b.updated_at || "").localeCompare(a.updated_at || "") || byName(a, b),
};

function allTags() {
  const counts = new Map();
  for (const r of state.recipes) for (const t of r.tags || []) counts.set(t, (counts.get(t) || 0) + 1);
  return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0], undefined, { sensitivity: "base", numeric: true }));
}

function visibleRecipes() {
  const q = view.search.trim().toLowerCase();
  const want = view.tags.map((t) => t.toLowerCase());
  return state.recipes.filter((r) => {
    if (view.host && String(r.host_id) !== view.host) return false;
    if (view.kind && kindOf(r) !== view.kind) return false;
    const tags = (r.tags || []).map((t) => t.toLowerCase());
    if (!want.every((t) => tags.includes(t))) return false;
    if (!q) return true;
    return [r.name, r.model, r.description, r.image, r.container_name, r.framework, hostName(r.host_id), ...tags]
      .some((v) => String(v || "").toLowerCase().includes(q));
  }).sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || (SORTS[view.sort] || byName)(a, b));
}

const kindOf = (r) => r.kind === "agent" ? "agent" : "model";

function renderToolbar() {
  $$("#recipe-kind button").forEach((b) => b.classList.toggle("on", b.dataset.kind === view.kind));
  $$("#recipe-layout button").forEach((b) => b.classList.toggle("on", b.dataset.layout === (view.layout === "tiles" ? "tiles" : "rows")));
  const tags = allTags();
  // Drop filters for tags that no longer exist.
  const known = new Set(tags.map(([t]) => t.toLowerCase()));
  view.tags = view.tags.filter((t) => known.has(t.toLowerCase()));
  const active = new Set(view.tags.map((t) => t.toLowerCase()));
  $("#tag-filter").innerHTML = tags.length
    ? `<button class="chip ${active.size ? "" : "on"}" data-tag="">All <span>${state.recipes.length}</span></button>` +
      tags.map(([t, n]) => `<button class="chip ${active.has(t.toLowerCase()) ? "on" : ""}" data-tag="${esc(t)}">${esc(t)} <span>${n}</span></button>`).join("")
    : `<span class="hint">Add tags to recipes (e.g. the model name) to filter and group them here.</span>`;
  $("#tag-list").innerHTML = tags.map(([t]) => `<option value="${esc(t)}">`).join("");
}

function fmtTps(v) { return v >= 100 ? Math.round(v).toLocaleString() : +v.toFixed(1); }

// Live numbers from the server's /metrics (rates since the previous poll), or else from its last
// stats line in the logs (see servermetrics.py / logstats.py); refreshed with the status poll.
const STALE_LOG_S = 30;  // vLLM prints a stats line every ~10 s while busy and none while idle

/* Per-recipe generation-throughput history (client side, sampled at the status poll). */
const sparkHist = new Map();
const SPARK_MAX = 40;   // ~3–4 min at the 5 s poll
function recordMetrics(recipes) {
  const now = Date.now();
  for (const r of recipes) {
    const v = r.metrics?.gen_tps;
    if (!RUNNING.includes(r.status) || v == null) continue;
    let a = sparkHist.get(r.id);
    if (!a) sparkHist.set(r.id, a = []);
    const last = a[a.length - 1];
    if (last && now - last.t < 3000) { last.v = v; last.t = now; }  // coalesce re-renders
    else a.push({ t: now, v });
    if (a.length > SPARK_MAX) a.shift();
  }
}
function sparkline(id, idle) {
  const a = sparkHist.get(id);
  if (!a || a.length < 3) return "";
  const w = 72, h = 16;
  const vs = a.map((p) => p.v);
  const max = Math.max(...vs), min = Math.min(...vs), range = max - min || 1;
  const pts = a.map((p, i) =>
    `${((i / (a.length - 1)) * (w - 2) + 1).toFixed(1)},${(h - 1.5 - ((p.v - min) / range) * (h - 3)).toFixed(1)}`).join(" ");
  const secs = Math.round((a[a.length - 1].t - a[0].t) / 1000);
  const tip = `Output tok/s over the last ~${fmtAge(secs)}\nnow ${fmtTps(a[a.length - 1].v)} · peak ${fmtTps(max)}`;
  return `<svg class="spark${idle ? " flat" : ""}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><title>${esc(tip)}</title><polyline points="${pts}"/></svg>`;
}

function metricsHtml(r) {
  const m = r.metrics;
  if (!m || !RUNNING.includes(r.status)) return "";
  const parts = [];
  if (m.prompt_tps != null) parts.push(`<span title="Prompt (input) throughput">${fmtTps(m.prompt_tps)} tok/s in</span>`);
  if (m.gen_tps != null) {
    const per = m.running > 1 && m.gen_tps ? ` (≈${fmtTps(m.gen_tps / m.running)}/req)` : "";
    parts.push(`<span title="Generation (output) throughput">${fmtTps(m.gen_tps)} tok/s out${per}</span>`);
  }
  if (m.running != null) parts.push(`<span title="Requests running / waiting">${m.running} req${m.waiting ? ` · +${m.waiting} queued` : ""}</span>`);
  const kvCls = m.kv_cache > 85 ? " hot" : m.kv_cache > 60 ? " warm" : "";
  if (m.kv_cache != null) parts.push(`<span class="kv${kvCls}" title="KV cache usage">${m.kv_cache}% KV</span>`);
  if (m.prefix_hit != null) parts.push(`<span title="Prefix cache hit rate">${m.prefix_hit}% hit</span>`);
  if (!parts.length) return "";
  const stale = m.source === "log" && m.age_s != null && m.age_s > STALE_LOG_S;
  if (stale) parts.push(`<span>${fmtAge(m.age_s)} ago</span>`);
  const title = m.source === "metrics" ? "Live from the server's /metrics, over the last poll interval"
    : `From the server's last stats line in the log${m.age_s != null ? ` (${fmtAge(m.age_s)} ago)` : ""}`;
  const idle = !(m.running > 0) && !(m.waiting > 0) && !(m.prompt_tps > 0) && !(m.gen_tps > 0) && !(m.kv_cache > 0);
  // While idle the zeros are noise: show one calm line; the sparkline still reveals the last activity.
  const body = idle
    ? `<span class="idle-word">idle</span>${m.kv_cache != null ? ` · <span class="kv${kvCls}">${m.kv_cache}% KV</span>` : ""}`
    : parts.join(" · ");
  return `<div class="card-metrics mono${stale ? " stale" : ""}${idle ? " idle" : ""}" title="${title}">${idle ? "💤" : "📈"} ${body}${sparkline(r.id, idle)}</div>`;
}

function fmtAge(s) { return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`; }

// What a model card shows for "Model": a container path like /model says nothing, so follow the volume
// it is mounted from and show that folder's name; the full path stays in the tooltip.
function modelShown(r) {
  const model = r.model || "";
  const w = r.weights_id && state.weights.find((x) => x.id === r.weights_id);
  if (w) {
    const path = w.locations[r.host_id];
    return { label: w.name, title: `Registered weights · ${path ? `on this host: ${path}` : "no path set on this host"}` };
  }
  if (r.runtime === "docker" && model.startsWith("/")) {
    for (const line of (r.volumes || "").split("\n")) {
      const [src, dst] = line.trim().split(":");
      if (!src || !dst || !(model === dst || model.startsWith(`${dst.replace(/\/$/, "")}/`))) continue;
      const host = src + model.slice(dst.replace(/\/$/, "").length);
      return { label: host.replace(/\/+$/, "").split("/").pop() || model, title: `${model} ← ${host}` };
    }
  }
  if (model.startsWith("/") || model.startsWith("~")) return { label: model.replace(/\/+$/, "").split("/").pop() || model, title: model };
  return { label: model || "–", title: model };
}
// The name clients send as "model" (vLLM/SGLang --served-model-name), when the recipe sets one.
function servedName(r) {
  return (r.args || "").match(/(?:^|\s)--served-model-name(?:=|\s+)(\S+)/)?.[1]?.replace(/^['"]|['"]$/g, "") || "";
}
// "vllm/vllm-openai:nightly-3f1bcfd8…" → short enough for a card, full in the tooltip.
function imageShort(image) {
  const [repo, tag = "latest"] = image.split(/:(?=[^/]*$)/);
  const t = tag.length > 18 ? `${tag.slice(0, 16)}…` : tag;
  return `${repo.split("/").pop()}:${t}`;
}

// A copy is named "<original> · <host>": with Blur on, its host part is masked like everywhere else.
function recipeTitle(r) {
  const host = hostName(r.host_id);
  return state.blur && r.copy_of && host ? r.name.split(host).join(maskAddr(host)) : r.name;
}

// Copies on other hosts: on the original how many and whether all match, on a copy whether it matches.
function copiesFact(r) {
  if (r.copy_of) {
    const o = state.recipes.find((x) => x.id === r.copy_of);
    const same = !r.differs?.length;
    return ["Copy of", `${esc(o?.name ?? "?")} ${same ? `<span class="ok" title="Same settings as the original">✓ in sync</span>`
      : `<span class="warn" title="Differs in: ${esc(r.differs.join(", "))}">⟳ differs</span>
         <button class="copy-mini" data-act="sync" title="Take the original's settings">sync</button>`}
      <button class="copy-mini" data-act="unlink" title="Make this an original of its own: syncing no longer changes it">unlink</button>`];
  }
  if (!r.copies?.length) return null;
  const copies = r.copies.map((id) => state.recipes.find((x) => x.id === id)).filter(Boolean);
  const stale = copies.filter((c) => c.differs?.length);
  const where = copies.map((c) => maskAddr(hostName(c.host_id))).join(", ");
  return ["Copies", `<span title="${esc(where)}">on ${copies.length} host${copies.length === 1 ? "" : "s"}</span> ${stale.length
    ? `<span class="warn" title="Differ: ${esc(stale.map((c) => maskAddr(hostName(c.host_id))).join(", "))}">⟳ ${stale.length} differ${stale.length === 1 ? "s" : ""}</span>
       <button class="copy-mini" data-act="sync" title="Give all copies this recipe's settings">sync all</button>`
    : `<span class="ok">✓ in sync</span>`}`];
}

function cardHtml(r) {
  const busy = state.busy.has(r.id);
  const running = RUNNING.includes(r.status);
  const fw = state.meta?.frameworks[r.framework]?.label ?? r.framework;
  const url = `http://${endpointHost(r.host_id)}:${r.host_port}`;
  const active = new Set(view.tags.map((t) => t.toLowerCase()));
  // Optional startup estimate: median of the recent measured trigger→ready times.
  const hist = state.startTimes[r.id] || [];
  const med = hist.length ? median(hist.slice(0, 8)) : null;
  const booting = !busy && (r.status === "starting" || r.status === "loading");
  let startInfo = "";
  if (booting) {
    const elapsed = (Date.now() - (startWatch.get(r.id) ?? Date.now())) / 1000;
    startInfo = `<span class="start-live mono" title="Time since the start was triggered">⏳ ${fmtDur(elapsed)}</span>`
      + (med ? `<span class="start-hint mono" title="Median time to ready over the last ${Math.min(hist.length, 8)} start${hist.length > 1 ? "s" : ""}">/ ~${fmtDur(med)}</span>`
        + `<span class="bar eta-bar" title="Elapsed vs. typical startup time"><span class="${elapsed > med * 1.5 ? "bar-fill warn" : "bar-fill"}" style="width:${Math.min(100, Math.round(elapsed / med * 100))}%"></span></span>` : "");
  } else if (med && !running) {
    startInfo = `<span class="start-hint mono" title="Median time from Start to ready (last ${Math.min(hist.length, 8)} start${hist.length > 1 ? "s" : ""})">⏱ ~${fmtDur(med)}</span>`;
  }
  const actions = `
          ${running
            ? `<button class="btn small stop" data-act="stop" ${busy ? "disabled" : ""}>Stop</button>
               <button class="btn small" data-act="restart" ${busy ? "disabled" : ""}>Restart</button>`
            : `<button class="btn small go" data-act="start" ${busy ? "disabled" : ""}>Start</button>
               <button class="btn small" data-act="switch" ${busy ? "disabled" : ""} title="Stop all other models on this host, free its memory, then start this one">Switch to</button>`}
          <span class="actions-gap"></span>
          <button class="btn small ghost" data-act="logs" ${r.container ? "" : "disabled"}>Logs</button>
          <button class="btn small ghost" data-act="edit">Edit</button>
          <button class="btn small ghost icon" data-act="duplicate" title="Duplicate" aria-label="Duplicate">⧉</button>
          ${multiHost() ? `<button class="btn small ghost icon" data-act="copyto" title="Copy to other hosts – kept in sync with this one" aria-label="Copy to other hosts">⇉</button>` : ""}
          <button class="btn small ghost danger icon" data-act="delete" ${running ? "disabled" : ""} title="${running ? "Stop it before deleting" : "Delete"}" aria-label="Delete">🗑</button>`;
  const endpoint = (running ? `<a href="${esc(url)}/v1/models" target="_blank" rel="noopener">${esc(maskEndpoint(url))}/v1</a>`
    : `<span class="muted">${esc(maskEndpoint(url))}/v1</span>`)
    + `<button class="copy-mini" data-act="copy" title="Copy the endpoint">copy</button>`;
  const model = modelShown(r);
  const served = servedName(r);
  const facts = [
    ["Model", `<span title="${esc(model.title)}">${esc(model.label)}</span>`],
    served && served !== model.label && served !== r.model ? ["Served as", `<code>${esc(served)}</code>`] : null,
    ["Endpoint", endpoint],
    r.runtime === "docker" && r.image ? ["Image", `<span title="${esc(r.image)}">${esc(imageShort(r.image))}</span>`] : null,
    r.est_memory_gb ? ["Memory", `~${r.est_memory_gb} GB`] : null,
    copiesFact(r),
    r.container ? [r.runtime === "process" ? "Process" : "Container",
      `<span title="${esc(r.container_name)}">${esc(r.container.status)}</span>`] : null,
  ].filter(Boolean);
  if (view.layout !== "tiles") {
    // Row layout: the same facts as the tile, side by side; agents keep their own row style.
    return `
      <article class="card model-row is-${r.status}" data-id="${r.id}" data-color="${esc(r.color || "")}" tabindex="0">
        <div class="row-icon" aria-hidden="true">🧠</div>
        <div class="row-main">
          <div class="row-title">
            <span class="card-title">${esc(recipeTitle(r))}</span>
            <span class="badge ${esc(r.framework)}">${esc(fw)}</span>
            ${multiHost() ? `<span class="card-host" title="Runs on this host">⌂ ${esc(maskAddr(hostName(r.host_id)))}</span>` : ""}
            <button class="btn small ghost pin${r.pinned ? " on" : ""}" data-act="pin" title="${r.pinned ? "Unpin: back to normal sort" : "Pin: always keep this recipe on top"}">${r.pinned ? "★" : "☆"}</button>
          </div>
          <div class="status-row"><span class="pill ${busy ? "busy" : r.status}">${busy ? "Working…" : STATUS_LABEL[r.status] ?? r.status}</span>${startInfo}
            ${r.tags?.length ? r.tags.map((t) =>
              `<button class="chip small ${active.has(t.toLowerCase()) ? "on" : ""}" data-tag="${esc(t)}" title="Filter by this tag">${esc(t)}</button>`).join("") : ""}</div>
          ${r.description ? `<div class="card-desc">${esc(r.description)}</div>` : ""}
          <dl class="row-facts">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>
          ${metricsHtml(r)}
        </div>
        <div class="actions">${actions}</div>
      </article>`;
  }
  return `
      <article class="card is-${r.status}" data-id="${r.id}" data-color="${esc(r.color || "")}" tabindex="0">
        <div class="card-head">
          <div class="card-title">${esc(recipeTitle(r))}</div>
          <div class="card-head-right">
            <button class="btn small ghost pin${r.pinned ? " on" : ""}" data-act="pin" title="${r.pinned ? "Unpin: back to normal sort" : "Pin: always keep this recipe on top"}">${r.pinned ? "★" : "☆"}</button>
            <span class="badge ${esc(r.framework)}">${esc(fw)}</span>
          </div>
        </div>
        ${multiHost() ? `<div class="card-host" title="Runs on this host">⌂ ${esc(maskAddr(hostName(r.host_id)))}</div>` : ""}
        <div class="status-row"><span class="pill ${busy ? "busy" : r.status}">${busy ? "Working…" : STATUS_LABEL[r.status] ?? r.status}</span>${startInfo}</div>
        ${r.tags?.length ? `<div class="card-tags">${r.tags.map((t) =>
          `<button class="chip small ${active.has(t.toLowerCase()) ? "on" : ""}" data-tag="${esc(t)}" title="Filter by this tag">${esc(t)}</button>`).join("")}</div>` : ""}
        ${r.description ? `<div class="card-desc">${esc(r.description)}</div>` : ""}
        <dl class="kv">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
        ${metricsHtml(r)}
        <div class="actions">${actions}</div>
      </article>`;
}

// Agents (e.g. a Hermes gateway) serve no LLM API: a wide row with what matters for them - running or
// not, the secrets they need, stray copies - instead of a model card's endpoint and engine numbers.
const AGENT_STATUS_LABEL = { ...STATUS_LABEL, ready: "Running", loading: "Starting" };

function agentCardHtml(r) {
  const busy = state.busy.has(r.id);
  const running = RUNNING.includes(r.status);
  const active = new Set(view.tags.map((t) => t.toLowerCase()));
  const uptime = r.container?.status ? `<span class="muted mono agent-uptime">${esc(r.container.status)}</span>` : "";
  const secrets = Object.entries(r.secrets || {});
  const dups = r.duplicates || [];
  return `
      <article class="card agent-card is-${r.status}" data-id="${r.id}" data-color="${esc(r.color || "")}" tabindex="0">
        <div class="agent-icon" aria-hidden="true">🤖</div>
        <div class="agent-main">
          <div class="agent-title-row">
            <span class="card-title">${esc(recipeTitle(r))}</span>
            <span class="badge agent" title="An agent: uses a model, kept running by “Switch to”">Agent · ${r.runtime === "process" ? "native" : "Docker"}</span>
            ${multiHost() ? `<span class="card-host" title="Runs on this host">⌂ ${esc(maskAddr(hostName(r.host_id)))}</span>` : ""}
            <button class="btn small ghost pin${r.pinned ? " on" : ""}" data-act="pin" title="${r.pinned ? "Unpin: back to normal sort" : "Pin: always keep this recipe on top"}">${r.pinned ? "★" : "☆"}</button>
          </div>
          <div class="status-row"><span class="pill ${busy ? "busy" : r.status}">${busy ? "Working…" : AGENT_STATUS_LABEL[r.status] ?? r.status}</span>${uptime}</div>
          ${r.description ? `<div class="card-desc">${esc(r.description)}</div>` : ""}
          <div class="agent-facts">
            ${r.model ? `<span class="agent-uses" title="The model this agent talks to">uses <b>${esc(r.model)}</b></span>` : ""}
            ${secrets.map(([k, ok]) => ok
              ? `<span class="chip small secret ok" title="Set in the manager's environment on this host (value hidden)">🔑 ${esc(k)} ✓</span>`
              : `<span class="chip small secret bad" title="Not set in the manager's environment on this host: the agent will not get it. Add it to the manager's .env and restart the manager.">🔑 ${esc(k)} ✗ not set</span>`).join("")}
            ${(r.tags || []).map((t) =>
              `<button class="chip small ${active.has(t.toLowerCase()) ? "on" : ""}" data-tag="${esc(t)}" title="Filter by this tag">${esc(t)}</button>`).join("")}
          </div>
          ${copiesFact(r) ? `<div class="agent-copies">${copiesFact(r)[0]}: ${copiesFact(r)[1]}</div>` : ""}
          ${dups.length ? `<div class="agent-warn">⚠ ${dups.length === 1 ? "Another copy" : `${dups.length} other copies`} of this command
            ${dups.length === 1 ? "runs" : "run"} outside the manager (pid ${dups.map(Number).join(", ")}). Two gateways on one bot
            token lose messages: kill ${dups.length === 1 ? "it" : "them"}, then restart this recipe.</div>` : ""}
        </div>
        <div class="actions">
          ${running
            ? `<button class="btn small stop" data-act="stop" ${busy ? "disabled" : ""}>Stop</button>
               <button class="btn small" data-act="restart" ${busy ? "disabled" : ""}>Restart</button>`
            : `<button class="btn small go" data-act="start" ${busy ? "disabled" : ""}>Start</button>`}
          <button class="btn small ghost" data-act="logs" ${r.container ? "" : "disabled"}>Logs</button>
          <button class="btn small ghost" data-act="edit">Edit</button>
          <button class="btn small ghost" data-act="duplicate">Duplicate</button>
          ${multiHost() ? `<button class="btn small ghost" data-act="copyto" title="Copy to other hosts – kept in sync with this one">Copy to hosts</button>` : ""}
          <button class="btn small ghost danger" data-act="delete" ${running ? "disabled" : ""}>Delete</button>
        </div>
      </article>`;
}

// A section head per kind; `hostId` (inside a host group) makes "+ New" create it on that host.
function kindHeadHtml(kind, items, hostId = "") {
  const running = items.filter((r) => RUNNING.includes(r.status)).length;
  const count = `${items.length} ${kind === "agent" ? "agent" : "model"}${items.length === 1 ? "" : "s"}${running ? ` · ${running} running` : ""}`;
  const add = (label) => `<button class="btn small" data-new-kind="${kind}" data-host="${esc(hostId)}">+ New ${label}</button>`;
  return kind === "agent"
    ? `<div class="kind-head agents"><h3>🤖 Agents <span>${count}</span></h3>
        <span class="kind-sub">Programs that use a model. “Switch to” keeps them running.</span>${add("agent")}</div>`
    : `<div class="kind-head models"><h3>🧠 Models <span>${count}</span></h3>
        <span class="kind-sub">LLM servers with an OpenAI-compatible API.</span>${add("model")}</div>`;
}

// Agents above models: as rows, then as the usual card grid.
function kindsHtml(items, hostId = "") {
  const agents = items.filter((r) => kindOf(r) === "agent");
  const models = items.filter((r) => kindOf(r) === "model");
  return (agents.length ? `<section class="kind-section agents">${kindHeadHtml("agent", agents, hostId)}
      <div class="agent-list">${agents.map(agentCardHtml).join("")}</div></section>` : "")
    + (models.length ? `<section class="kind-section models">${kindHeadHtml("model", models, hostId)}
      <div class="${view.layout === "tiles" ? "grid" : "row-list"}">${models.map(cardHtml).join("")}</div></section>` : "");
}

function renderRecipes() {
  renderToolbar();
  if (!state.recipes.length) {
    $("#recipes").innerHTML = `<div class="empty">No recipes yet. Click “Add recipe”.</div>`;
    return;
  }
  const list = visibleRecipes();
  if (!list.length) {
    $("#recipes").innerHTML = `<div class="empty">No recipe matches the current filter.</div>`;
    return;
  }
  let html = "";
  if (!view.kind && !state.recipes.some((r) => kindOf(r) === "agent")) {
    html += `<div class="agent-empty">🤖 No agents yet. Agents, such as a Hermes gateway, run as recipes too and get
      their own section here. <button class="btn small ghost" data-new-kind="agent">+ New agent</button></div>`;
  }
  $("#recipes").innerHTML = html + (view.group ? groupedHtml(list) : kindsHtml(list));
}

// Group by host, or by each recipe's first tag (e.g. the model name); every recipe appears once, and
// each group stacks its agents above its models.
function groupedHtml(list) {
  const groups = new Map();
  for (const r of list) {
    const label = view.group === "host" ? maskAddr(hostName(r.host_id)) : r.tags?.[0] ?? "";
    const key = view.group === "host" ? String(state.hosts.findIndex((h) => h.id === r.host_id)).padStart(3, "0") : label.toLowerCase();
    if (!groups.has(key)) groups.set(key, { label, hostId: view.group === "host" ? r.host_id : "", items: [] });
    groups.get(key).items.push(r);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b, undefined, { numeric: true }));
  return keys.map((k) => {
    const g = groups.get(k);
    const running = g.items.filter((r) => RUNNING.includes(r.status)).length;
    const nAgents = g.items.filter((r) => kindOf(r) === "agent").length;
    const nModels = g.items.length - nAgents;
    const parts = [nAgents && `${nAgents} agent${nAgents === 1 ? "" : "s"}`, nModels && `${nModels} model${nModels === 1 ? "" : "s"}`].filter(Boolean);
    const collapsed = !!view.collapsed[`${view.group}:${k}`];
    return `<section class="group${collapsed ? " is-collapsed" : ""}" data-gkey="${esc(k)}">
        <h3 class="group-title" title="Click to ${collapsed ? "expand" : "collapse"}"><span class="group-caret">${collapsed ? "▸" : "▾"}</span>${view.group === "host" ? "⌂ " : ""}${g.label ? esc(g.label) : "Untagged"} <span>${parts.join(" · ")}${running ? ` · ${running} running` : ""}</span></h3>
        <div class="group-body">${kindsHtml(g.items, g.hostId)}</div>
      </section>`;
  }).join("");
}

function toggleTagFilter(tag) {
  if (!tag) view.tags = [];
  else if (view.tags.some((t) => t.toLowerCase() === tag.toLowerCase())) view.tags = view.tags.filter((t) => t.toLowerCase() !== tag.toLowerCase());
  else view.tags = [...view.tags, tag];
  saveView();
  renderRecipes();
}

$("#tag-filter").addEventListener("click", (ev) => {
  const chip = ev.target.closest("[data-tag]");
  if (chip) toggleTagFilter(chip.dataset.tag);
});
$("#recipe-search").addEventListener("input", (ev) => { view.search = ev.target.value; saveView(); renderRecipes(); });
$("#recipe-sort").addEventListener("change", (ev) => { view.sort = ev.target.value; saveView(); renderRecipes(); });
$("#recipe-group").addEventListener("change", (ev) => { view.group = ev.target.value; saveView(); renderRecipes(); });
$("#recipe-layout").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-layout]");
  if (!b) return;
  view.layout = b.dataset.layout;
  saveView();
  renderRecipes();
});
$("#recipe-kind").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-kind]");
  if (!b) return;
  view.kind = b.dataset.kind;
  saveView();
  renderRecipes();
});
$("#recipe-host").addEventListener("change", (ev) => { view.host = ev.target.value; saveView(); renderRecipes(); });
$("#recipe-search").value = view.search;
$("#recipe-sort").value = SORTS[view.sort] ? view.sort : "name";
$("#recipe-group").value = ["host", "tag"].includes(view.group) ? view.group : "";

async function runAction(key, path, okMsg) {
  state.busy.add(key);
  renderRecipes();
  try {
    const res = await api(path, { method: "POST" });
    toast(okMsg ?? res?.message ?? "Done", "ok");
    return true;
  } catch (e) {
    toast(e.message, "bad");
    return e;
  } finally {
    state.busy.delete(key);
    await loadRecipes();
    loadHosts();
  }
}

const RECIPE_BODY_FIELDS = ["name", "description", "host_id", "runtime", "framework", "image", "model",
  "container_name", "host_port", "container_port", "command", "args", "env", "volumes", "docker_args",
  "gpus", "health_path", "est_memory_gb", "tags", "color", "kind"];
function recipeBodyFrom(r, overrides = {}) {
  const body = { pinned: !!r.pinned };
  for (const f of RECIPE_BODY_FIELDS) body[f] = r[f];
  return { ...body, ...overrides };
}

$("#recipes").addEventListener("click", async (ev) => {
  const add = ev.target.closest("[data-new-kind]");
  if (add) { openRecipeEditor(null, add.dataset.newKind, add.dataset.host); return; }
  const head = ev.target.closest(".group-title");
  if (head) {
    const key = head.closest(".group").dataset.gkey;
    if (ev.shiftKey) {
      // Shift+click: collapse every group, or expand them all when this one is collapsed.
      const anyOpen = $$(".group", $("#recipes")).some((s) => !s.classList.contains("is-collapsed"));
      for (const s of $$(".group", $("#recipes"))) {
        view.collapsed[`${view.group}:${s.dataset.gkey}`] = anyOpen;
      }
    } else {
      const id = `${view.group}:${key}`;
      view.collapsed[id] = !view.collapsed[id];
    }
    saveView();
    renderRecipes();
    return;
  }
  const chip = ev.target.closest(".card [data-tag]");
  if (chip) { toggleTagFilter(chip.dataset.tag); return; }
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.closest(".card").dataset.id);
  const r = state.recipes.find((x) => x.id === id);
  const act = btn.dataset.act;
  if (act === "start") {
    const res = await runAction(id, `/api/recipes/${id}/start`);
    if (res instanceof Error && /memory/i.test(res.message) && confirm(`${res.message}\n\nStart anyway?`)) {
      await runAction(id, `/api/recipes/${id}/start?force=true`);
    }
  } else if (act === "stop") {
    await runAction(id, `/api/recipes/${id}/stop`);
  } else if (act === "restart") {
    await runAction(id, `/api/recipes/${id}/restart`);
  } else if (act === "switch") {
    if (confirm(`Stop all other models, free memory and start “${r.name}”?`)) await runAction(id, `/api/recipes/${id}/switch`);
  } else if (act === "logs") {
    openLogs(r);
  } else if (act === "edit") {
    openRecipeEditor(r);
  } else if (act === "copy") {
    const ep = endpointUrl(r);
    try { await navigator.clipboard.writeText(ep); toast("Endpoint copied", "ok"); }
    catch { prompt("Copy the endpoint:", ep); }
  } else if (act === "duplicate") {
    try {
      const copy = await api(`/api/recipes/${id}/duplicate`, { method: "POST" });
      await loadRecipes();
      openRecipeEditor(copy);
    } catch (e) { toast(e.message, "bad"); }
  } else if (act === "copyto") {
    openCopyTo(r);
  } else if (act === "sync") {
    await syncCopies(r.id);
  } else if (act === "unlink") {
    if (!confirm(`Unlink “${r.name}” from its original? It keeps its settings, and syncing no longer changes it.`)) return;
    try { await api(`/api/recipes/${id}/unlink`, { method: "POST" }); await loadRecipes(); } catch (e) { toast(e.message, "bad"); }
  } else if (act === "pin") {
    try {
      await api(`/api/recipes/${id}`, { method: "PUT", body: recipeBodyFrom(r, { pinned: !r.pinned }) });
      await loadRecipes();
    } catch (e) { toast(e.message, "bad"); }
  } else if (act === "delete") {
    if (!confirm(`Delete recipe “${r.name}”? Schedules using it are deleted too.`)) return;
    try {
      await api(`/api/recipes/${id}`, { method: "DELETE" });
      toast("Recipe deleted", "ok");
      loadRecipes();
    } catch (e) { toast(e.message, "bad"); }
  }
});

/* ---------------- recipe editor ---------------- */

const recipeForm = $("#form-recipe");
let editingRecipe = null;
let previewTimer = null;

function recipeFromForm() {
  const f = new FormData(recipeForm);
  const data = Object.fromEntries(f.entries());
  data.host_port = Number(data.host_port) || 0;
  data.container_port = Number(data.container_port) || 0;
  data.est_memory_gb = data.est_memory_gb === "" ? null : Number(data.est_memory_gb);
  data.host_id = data.host_id ? Number(data.host_id) : null;
  data.tags = (data.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
  data.weights_id = data.weights_id ? Number(data.weights_id) : null;
  return data;
}

function fillRecipeForm(r) {
  // Runtime first: it decides which frameworks are selectable.
  if (r.runtime) {
    recipeForm.elements.runtime.value = r.runtime;
    renderFrameworkOptions(r.runtime);
  }
  for (const el of recipeForm.elements) {
    if (el.name && el.name in r) el.value = Array.isArray(r[el.name]) ? r[el.name].join(", ") : r[el.name] ?? "";
  }
  syncRuntime();
  markSwatch();
}

function renderFrameworkOptions(runtime) {
  const sel = $("#sel-framework");
  const current = sel.value;
  sel.innerHTML = Object.entries(state.meta.frameworks)
    .filter(([, v]) => v.runtime === runtime)
    .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

// Native processes run directly on the host: no image, volumes, docker flags or second port.
function syncRuntime() {
  const native = recipeForm.elements.runtime.value === "process";
  $$(".docker-only", recipeForm).forEach((el) => el.classList.toggle("hidden", native));
  $("#name-label").textContent = native ? "Process name" : "Container name";
  $("#port-label").textContent = native ? "Port" : "Host port";
  $("#model-hint").textContent = native ? "HF id or local path (~ allowed)" : "HF id or path inside the container";
  if (native) recipeForm.elements.container_port.value = recipeForm.elements.host_port.value;
  if (recipeForm.elements.kind.value === "agent") setKind("agent", false);
}

// Agents have no LLM API: by default no GPU (`--gpus all` fails on machines without NVIDIA) and no
// health check (the port is only a placeholder), so "running" means the process/container is up.
function setKind(kind, adjust) {
  const f = recipeForm.elements;
  f.kind.value = kind;
  $$("#kind-seg button").forEach((b) => b.classList.toggle("on", b.dataset.kind === kind));
  const agent = kind === "agent";
  if (adjust) {
    const preset = state.meta.frameworks[f.framework.value] || {};
    if (agent) {
      if (f.gpus.value.trim() === "all") f.gpus.value = "";
      if (f.health_path.value === (preset.health_path ?? "")) f.health_path.value = "";
    } else if (!f.health_path.value) {
      f.health_path.value = preset.health_path ?? "";
    }
  }
  $("#model-hint").textContent = agent ? "the model it talks to, shown on the card (optional)"
    : f.runtime.value === "process" ? "HF id or local path (~ allowed)" : "HF id or path inside the container";
  f.health_path.placeholder = agent ? "none: running = process/container is up" : "/health";
  $("#port-label").textContent = agent ? "Port (placeholder if it listens on none)" : f.runtime.value === "process" ? "Port" : "Host port";
  const noun = agent ? "agent" : "recipe";
  $("#recipe-title").textContent = editingRecipe ? `Edit ${noun} – ${editingRecipe.name}` : `New ${noun}`;
}

$("#kind-seg").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-kind]");
  if (!b || b.dataset.kind === recipeForm.elements.kind.value) return;
  setKind(b.dataset.kind, true);
  recipeForm.dispatchEvent(new Event("input"));
});

function markSwatch() {
  const current = recipeForm.elements.color.value;
  $$("#swatches .swatch").forEach((b) => b.classList.toggle("on", b.dataset.color === current));
}

$("#swatches").addEventListener("click", (ev) => {
  const b = ev.target.closest(".swatch");
  if (!b) return;
  recipeForm.elements.color.value = b.dataset.color;
  markSwatch();
});

function applyFramework(name, overwriteAll) {
  const p = state.meta.frameworks[name];
  recipeForm.elements.runtime.value = p.runtime;
  renderFrameworkOptions(p.runtime);
  recipeForm.elements.framework.value = name;
  const cur = recipeFromForm();
  const set = (k, v) => { if (overwriteAll || !cur[k]) recipeForm.elements[k].value = v ?? ""; };
  ["image", "command", "container_port", "health_path"].forEach((k) => { recipeForm.elements[k].value = p[k] ?? ""; });
  ["args", "docker_args", "volumes", "env"].forEach((k) => set(k, p[k]));
  syncRuntime();
  setKind(recipeForm.elements.kind.value, true);
  scheduleImageStatus();
}

// Ports are per host: the same port may be used on two different machines.
function recipesOnPort(hostId, port, exceptId) {
  return state.recipes.filter((r) => r.host_id === Number(hostId) && r.host_port === Number(port) && r.id !== exceptId);
}

function nextFreePort(hostId) {
  const used = new Set(state.recipes.filter((r) => r.host_id === Number(hostId)).map((r) => r.host_port));
  let p = 8100;
  while (used.has(p)) p++;
  return p;
}

function updatePortHint() {
  const f = recipeForm.elements;
  const hint = $("#port-hint");
  const port = Number(f.host_port.value);
  if (!port) { hint.textContent = ""; hint.className = "port-hint"; return; }
  const others = recipesOnPort(f.host_id.value, port, editingRecipe?.id);
  const where = multiHost() ? ` on ${maskAddr(hostName(f.host_id.value))}` : "";
  if (!others.length) {
    hint.textContent = `No other recipe uses port ${port}${where}.`;
    hint.className = "port-hint ok";
    return;
  }
  const running = others.filter((r) => RUNNING.includes(r.status));
  hint.textContent = running.length
    ? `Port ${port}${where} is in use by running “${running.map((r) => r.name).join("”, “")}” – this recipe can't start while it runs.`
    : `Also used by “${others.map((r) => r.name).join("”, “")}”${where} – only one of them can run at a time.`;
  hint.className = `port-hint ${running.length ? "bad" : "warn"}`;
}

$("#btn-next-port").addEventListener("click", () => {
  recipeForm.elements.host_port.value = nextFreePort(recipeForm.elements.host_id.value);
  recipeForm.dispatchEvent(new Event("input"));
});

function endpointUrl(r) { return `http://${endpointHost(r.host_id)}:${r.host_port}/v1`; }

function renderPorts() {
  const order = (r) => state.hosts.findIndex((h) => h.id === r.host_id);
  const rows = [...state.recipes].sort((a, b) => order(a) - order(b) || a.host_port - b.host_port || byName(a, b));
  $("#ports").innerHTML = rows.length ? rows.map((r) => {
    const shared = recipesOnPort(r.host_id, r.host_port, r.id);
    const running = RUNNING.includes(r.status);
    if (kindOf(r) === "agent") {
      return `<tr>
      <td>${esc(maskAddr(hostName(r.host_id)))}</td>
      <td class="mono">${r.host_port}${shared.length ? ` <span class="chip small warn-chip" title="Also used by: ${esc(shared.map((x) => x.name).join(", "))}">shared</span>` : ""}</td>
      <td>🤖 ${esc(r.name)}</td>
      <td><span class="pill ${r.status}">${AGENT_STATUS_LABEL[r.status] ?? r.status}</span></td>
      <td class="muted">agent – no LLM API</td>
    </tr>`;
    }
    return `<tr>
      <td>${esc(maskAddr(hostName(r.host_id)))}</td>
      <td class="mono">${r.host_port}${shared.length ? ` <span class="chip small warn-chip" title="Also used by: ${esc(shared.map((x) => x.name).join(", "))}">shared</span>` : ""}</td>
      <td>${esc(r.name)}</td>
      <td><span class="pill ${r.status}">${STATUS_LABEL[r.status] ?? r.status}</span></td>
      <td class="mono">${running ? `<a href="${esc(endpointUrl(r))}/models" target="_blank" rel="noopener">${esc(maskEndpoint(endpointUrl(r)))}</a>` : `<span class="muted">${esc(maskEndpoint(endpointUrl(r)))}</span>`}
        <button class="btn small ghost copy" data-copy="${esc(endpointUrl(r))}" title="Copy endpoint">Copy</button></td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">No recipes yet.</td></tr>`;
}

$("#ports").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-copy]");
  if (!b) return;
  try { await navigator.clipboard.writeText(b.dataset.copy); toast("Endpoint copied", "ok"); }
  catch { prompt("Copy the endpoint:", b.dataset.copy); }
});

function openRecipeEditor(r, kind = "model", hostId = "") {
  editingRecipe = r;
  recipeForm.reset();
  $("#sel-host").innerHTML = state.hosts.map((h) =>
    `<option value="${h.id}">${esc(maskAddr(h.name))}${h.info ? ` (${esc(PLATFORM[h.info.platform] ?? h.info.platform)})` : h.online ? "" : " (offline)"}</option>`).join("");
  $("#sel-host").closest("label").classList.toggle("hidden", !multiHost());
  $("#recipe-error").textContent = "";
  showRecipeNote([]);
  recipeForm.elements.color.value = "";
  markSwatch();
  setKind(r ? kindOf(r) : kind, false);
  renderWeightOptions();
  if (r) {
    fillRecipeForm(r);
    setKind(kindOf(r), false);
  } else {
    hostId = hostId || view.host || state.hosts.find((h) => h.is_local)?.id;
    recipeForm.elements.host_id.value = hostId ?? "";
    // On a Mac, containers have no GPU access, so a native process is the sensible default.
    const mac = hostPlatform(hostId) === "darwin";
    recipeForm.elements.gpus.value = mac ? "" : "all";
    recipeForm.elements.host_port.value = nextFreePort(recipeForm.elements.host_id.value);
    // A new agent starts as a native process (e.g. `hermes gateway run`); Docker is one click away.
    applyFramework(kind === "agent" || mac ? "process-custom" : "vllm", true);
  }
  // A new recipe usually starts from a command that already works: offer importing it first.
  $("#import-cta").classList.toggle("hidden", !!r);
  updateWeightsStatus();
  scheduleImageStatus();
  syncHostNetworkPorts();
  updatePortHint();
  updatePreview();
  $("#dlg-recipe").showModal();
}

$("#btn-add-recipe").addEventListener("click", () => { openRecipeEditor(null); openImport(); });

$("#sel-host").addEventListener("change", (e) => {
  updatePortHint();
  updateWeightsStatus();
  scheduleImageStatus();
  // New recipe moved to a Mac: switch to a native process (Docker on macOS has no GPU access).
  if (!editingRecipe && hostPlatform(e.target.value) === "darwin" && recipeForm.elements.runtime.value === "docker"
      && recipeForm.elements.kind.value !== "agent") {
    recipeForm.elements.gpus.value = "";
    applyFramework("process-custom", true);
    updatePreview();
  }
});

$("#sel-runtime").addEventListener("change", (e) => {
  const first = Object.entries(state.meta.frameworks).find(([, v]) => v.runtime === e.target.value)[0];
  const overwrite = !editingRecipe || confirm("Replace arguments and env with this framework's defaults too?");
  applyFramework(first, overwrite);
  updatePreview();
});

$("#sel-framework").addEventListener("change", (e) => {
  const overwrite = !editingRecipe || confirm("Replace arguments, flags, volumes and env with this framework's defaults too?");
  applyFramework(e.target.value, overwrite);
  updatePreview();
});

// With --network host the server listens on the host port directly; keep the two ports equal.
function syncHostNetworkPorts() {
  const f = recipeForm.elements;
  const hostNet = f.runtime.value === "process" || /(^|\s)--(network|net)(=|\s+)host(\s|$)/.test(f.docker_args.value);
  f.container_port.readOnly = hostNet;
  f.container_port.title = hostNet ? "Same as the host port because of --network host" : "";
  if (hostNet) f.container_port.value = f.host_port.value;
}

recipeForm.addEventListener("input", () => {
  syncHostNetworkPorts();
  updatePortHint();
  scheduleImageStatus();
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 250);
});

// One flag per line; docker's own flags (up to the image) and the framework's (after it) in two colours.
function previewHtml(command, image) {
  let inDocker = command.startsWith("docker ") && !!image;
  return command.replace(/ (--?[a-zA-Z])/g, " \\\n  $1").split("\n").map((line) => {
    const cont = / \\$/.test(line);
    const body = cont ? line.slice(0, -2) : line;
    const m = body.match(/^(\s*)(--?[\w.-]+)(.*)$/);
    const html = m ? `${m[1]}<span class="${inDocker ? "dflag" : "flag"}">${esc(m[2])}</span>${esc(m[3])}` : esc(body);
    if (inDocker && ` ${body} `.includes(` ${image} `)) inDocker = false;  // the image ends docker's flags
    return html + (cont ? ' <span class="cont">\\</span>' : "");
  }).join("\n");
}

async function updatePreview() {
  const id = editingRecipe?.id;
  try {
    const res = await api(`/api/recipes/preview${id ? `?recipe_id=${id}` : ""}`, { method: "POST", body: recipeFromForm() });
    $("#recipe-preview").dataset.command = res.command;
    $("#recipe-preview").innerHTML = previewHtml(res.command, recipeForm.elements.image.value.trim());
  } catch (e) {
    $("#recipe-preview").dataset.command = "";
    $("#recipe-preview").textContent = `(incomplete) ${e.message}`;
  }
}

$("#btn-copy-preview").addEventListener("click", async () => {
  const cmd = $("#recipe-preview").dataset.command;
  if (!cmd) return;
  try { await navigator.clipboard.writeText(cmd); toast("Command copied", "ok"); }
  catch { prompt("Copy the command:", cmd); }
});

/* Compact recipe files: the editor's fields as JSON/YAML, for another manager or examples/. */
let exportFormat = "yaml";
let exportSeq = 0;

async function refreshExport() {
  const seq = ++exportSeq;
  const fw = recipeForm.elements.framework.value;
  $("#export-framework").textContent = state.meta?.frameworks?.[fw]?.label || fw || "framework";
  $("#export-text").value = "";
  $("#export-error").textContent = "";
  const id = editingRecipe ? `&recipe_id=${editingRecipe.id}` : "";
  try {
    const text = await api(`/api/recipes/export?format=${exportFormat}${id}`, { method: "POST", body: recipeFromForm() });
    if (seq !== exportSeq) return;
    $("#export-text").value = typeof text === "string" ? text : JSON.stringify(text, null, 2);
    $("#export-error").textContent = "";
  } catch (e) {
    if (seq !== exportSeq) return;
    $("#export-text").value = "";
    $("#export-error").textContent = e.message;
  }
}

$("#btn-export-recipe").addEventListener("click", () => {
  $("#dlg-export").showModal();
  refreshExport();
});

$("#export-format").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-format]");
  if (!b || b.dataset.format === exportFormat) return;
  exportFormat = b.dataset.format;
  $$("#export-format button").forEach((x) => x.classList.toggle("on", x === b));
  refreshExport();
});

$("#btn-export-copy").addEventListener("click", async () => {
  const text = $("#export-text").value;
  if (!text) return;
  try { await navigator.clipboard.writeText(text); toast("Recipe file copied", "ok"); }
  catch { prompt("Copy the recipe file:", text); }
});

$("#btn-export-download").addEventListener("click", () => {
  const text = $("#export-text").value;
  if (!text) return;
  const ext = exportFormat === "json" ? "json" : "yaml";
  const name = (recipeForm.elements.name.value || "recipe").replace(/[^a-zA-Z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "");
  const blob = new Blob([text], { type: exportFormat === "json" ? "application/json" : "text/yaml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${name || "recipe"}.${ext}`;
  a.click();
  URL.revokeObjectURL(a.href);
});

recipeForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = recipeFromForm();
  try {
    let saved;
    if (editingRecipe) {
      saved = await api(`/api/recipes/${editingRecipe.id}`, { method: "PUT", body });
    } else {
      saved = await api("/api/recipes", { method: "POST", body });
    }
    $("#dlg-recipe").close();
    toast("Recipe saved", "ok");
    const copies = editingRecipe ? state.recipes.filter((x) => x.copy_of === editingRecipe.id) : [];
    if (copies.length && confirm(`“${saved.name}” has ${copies.length} cop${copies.length === 1 ? "y" : "ies"} on other hosts. Update ${copies.length === 1 ? "it" : "them"} too?`)) {
      await syncCopies(saved.id);
    }
    const wasRunning = editingRecipe && ["ready", "loading", "starting"].includes(editingRecipe.status);
    if (wasRunning && confirm("This model is running. Restart it now so the changes take effect?")) {
      await runAction(saved.id, `/api/recipes/${saved.id}/restart`);
    } else {
      loadRecipes();
    }
  } catch (e) {
    $("#recipe-error").textContent = e.message;
  }
});

/* ---------------- weights registry ---------------- */

async function loadWeightsList() {
  try { state.weights = (await api("/api/weights?check=false")).weights; } catch { return; }
  renderWeightOptions();
}

function renderWeightOptions() {
  const sel = $("#sel-weights");
  const cur = sel.value;
  sel.innerHTML = `<option value="">None – paths are written in Volumes / Model</option>`
    + state.weights.map((w) => `<option value="${w.id}">${esc(w.name)}</option>`).join("");
  sel.value = state.weights.some((w) => String(w.id) === cur) ? cur : "";
}

const usesWeightsField = (data) => ["model", "volumes", "command", "args", "env"].some((f) => (data[f] || "").includes("{weights}"));

// Picking weights for a recipe that does not say where they go yet: mount them where the model is read.
function applyWeightsSelection() {
  const f = recipeForm.elements;
  if (!f.weights_id.value || usesWeightsField(recipeFromForm())) return;
  if (f.runtime.value === "process") { f.model.value = "{weights}"; return; }
  if (!f.model.value.trim().startsWith("/")) f.model.value = "/model";
  const dst = f.model.value.trim().replace(/\/+$/, "");
  const lines = f.volumes.value.split("\n");
  const i = lines.findIndex((l) => l.trim().split(":")[1]?.replace(/\/+$/, "") === dst);
  if (i >= 0) {
    const [, d, ...rest] = lines[i].trim().split(":");
    lines[i] = ["{weights}", d, ...rest].join(":");
  } else {
    lines.unshift(`{weights}:${dst}:ro`);
  }
  f.volumes.value = lines.filter((l, j) => l.trim() || j < lines.length - 1).join("\n");
}

// After an import: if the model's own mount is the path a registered entry has on this host, switch the
// recipe to that entry. Returns the entry's name, or "".
function useRegisteredWeights() {
  const f = recipeForm.elements;
  if (f.runtime.value !== "docker" || f.weights_id.value) return "";
  const dst = f.model.value.trim().replace(/\/+$/, "");
  if (!dst.startsWith("/")) return "";
  const lines = f.volumes.value.split("\n");
  const i = lines.findIndex((l) => l.trim().split(":")[1]?.replace(/\/+$/, "") === dst);
  if (i < 0) return "";
  const src = lines[i].trim().split(":")[0].replace(/\/+$/, "");
  const hostId = Number(f.host_id.value) || state.hosts.find((h) => h.is_local)?.id;
  const w = state.weights.find((x) => (x.locations[hostId] || "").replace(/\/+$/, "") === src);
  if (!w) return "";
  const [, d, ...rest] = lines[i].trim().split(":");
  lines[i] = ["{weights}", d, ...rest].join(":");
  f.volumes.value = lines.join("\n");
  f.weights_id.value = String(w.id);
  return w.name;
}

let weightsCheckSeq = 0;
async function updateWeightsStatus() {
  const box = $("#weights-status");
  const f = recipeForm.elements;
  const id = Number(f.weights_id.value);
  const hostId = Number(f.host_id.value) || state.hosts.find((h) => h.is_local)?.id;
  const seq = ++weightsCheckSeq;
  const show = (cls, html) => { if (seq === weightsCheckSeq) { box.className = `port-hint ${cls}`; box.innerHTML = html; } };
  if (!id) {
    show(usesWeightsField(recipeFromForm()) ? "bad" : "", usesWeightsField(recipeFromForm())
      ? "✗ This recipe uses {weights} but no weights are selected." : "");
    return;
  }
  const host = esc(maskAddr(hostName(hostId)));
  const fix = `<button type="button" class="link" data-edit-weight="${id}" data-host="${hostId}">set the path</button>`;
  show("", `Checking ${host}…`);
  try {
    const c = await api(`/api/weights/${id}/hosts/${hostId}`);
    if (c.state === "present") show("ok", `✓ on ${host}: <code>${esc(c.path)}</code>`);
    else if (c.state === "unset") show("bad", `✗ No path for these weights on ${host} – ${fix}`);
    else if (c.state === "missing") show("bad", `✗ Not found on ${host} at <code>${esc(c.location || c.path)}</code> – copy them there or ${fix}`);
    else show("warn", `? ${host} could not check <code>${esc(c.path)}</code>${c.error ? ` (${esc(blurHostText(c.error, hostById(hostId)?.url))})` : ""}`);
  } catch (e) {
    show("warn", esc(e.message));
  }
}

let imageCheckSeq = 0;
let imageCheckTimer = null;

async function updateImageStatus() {
  const box = $("#image-status");
  const f = recipeForm.elements;
  const image = f.image.value.trim();
  const hostId = Number(f.host_id.value) || state.hosts.find((h) => h.is_local)?.id;
  const seq = ++imageCheckSeq;
  if (f.runtime.value !== "docker" || !image || !hostId) {
    box.textContent = "";
    box.className = "port-hint";
    return;
  }
  const host = esc(maskAddr(hostName(hostId)));
  box.textContent = `Checking ${host}…`;
  box.className = "port-hint";
  try {
    const c = await api(`/api/images/check?host_id=${hostId}&image=${encodeURIComponent(image)}`);
    if (seq !== imageCheckSeq) return;
    if (c.state === "present") {
      const meta = c.local?.created ? ` · ${imageAge(c.local.created, true)}` : c.local?.size ? ` · ${c.local.size}` : "";
      box.innerHTML = `✓ on ${host}${esc(meta)}`;
      box.className = "port-hint ok";
    } else if (c.state === "missing") {
      const others = (c.other_tags || []).map((t) => t.tag);
      const more = others.length ? ` (local: ${esc(others.slice(0, 4).join(", "))}${others.length > 4 ? ", …" : ""})` : "";
      box.innerHTML = `✗ not on ${host} – Docker pulls it on first start${more}`;
      box.className = "port-hint warn";
    } else {
      box.innerHTML = `? ${host} could not check${c.error ? ` (${esc(blurHostText(c.error, hostById(hostId)?.url))})` : ""}`;
      box.className = "port-hint warn";
    }
  } catch (e) {
    if (seq !== imageCheckSeq) return;
    box.textContent = `? ${e.message}`;
    box.className = "port-hint warn";
  }
}

function scheduleImageStatus() {
  clearTimeout(imageCheckTimer);
  imageCheckTimer = setTimeout(updateImageStatus, 350);
}

$("#sel-weights").addEventListener("change", () => {
  applyWeightsSelection();
  syncHostNetworkPorts();
  updateWeightsStatus();
  updatePreview();
});
$("#weights-status").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-edit-weight]");
  if (b) openWeightEditor(state.weights.find((w) => w.id === Number(b.dataset.editWeight)), Number(b.dataset.host));
});

/* Weights tab: the registry, one column per host */

let weightsData = null;

async function loadWeights() {
  const tbody = $("#weights");
  if (!weightsData) tbody.innerHTML = `<tr><td class="empty">Asking the hosts…</td></tr>`;
  try {
    weightsData = await api("/api/weights");
  } catch (e) {
    tbody.innerHTML = `<tr><td class="empty">${esc(e.message)}</td></tr>`;
    return;
  }
  renderWeights();
}

function weightCellHtml(w, c) {
  const host = hostById(c.host_id);
  const title = c.state === "unknown" ? blurHostText(c.error || "not reachable", host?.url)
    : c.state === "missing" ? `Not found at ${c.location || c.path}` : c.state === "present" ? c.location || c.path : "";
  const icon = { present: `<span class="ok">✓</span>`, missing: `<span class="bad">✗</span>`, unknown: `<span class="muted">?</span>` }[c.state];
  const size = c.state === "present" && c.size_gb != null ? `<span class="muted" title="Size on this host">${c.size_gb} GB</span>` : "";
  const body = c.path ? `${icon} <code>${esc(c.path)}</code>${size}` : `<span class="muted">+ set path</span>`;
  return `<button type="button" class="weight-cell ${c.state}" data-weight="${w.id}" data-host="${c.host_id}" title="${esc(title)}">${body}</button>`;
}

function renderWeights() {
  if (!weightsData) return;
  const hosts = state.hosts;
  $("#weights-head").innerHTML = `<tr><th>Weights</th><th>Recipes</th>${hosts.map((h) => `<th>${esc(maskAddr(h.name))}</th>`).join("")}</tr>`;
  const list = weightsData.weights;
  if (!list.length) {
    $("#weights").innerHTML = `<tr><td colspan="${2 + hosts.length}" class="empty">Nothing registered yet. “+ Register weights” adds an entry; then pick it in a recipe's Weights field.</td></tr>`;
    return;
  }
  $("#weights").innerHTML = list.map((w) => `<tr>
      <td><button type="button" class="link weight-name" data-weight="${w.id}" title="Edit name, source and paths">${esc(w.name)}</button>
        ${w.source ? `<div class="muted mono weight-source">${esc(w.source)}</div>` : ""}
        <span class="weight-actions">
          ${w.hosts.some((c) => c.state === "present") && state.hosts.length > 1 ? `<button class="btn small ghost" data-copy-weight="${w.id}" title="Copy from the hosts that have it to the others, over your network">⇉ Copy…</button>` : ""}
          <button class="btn small ghost" data-download-weight="${w.id}" title="Download from Hugging Face on the hosts that don't have it">⬇ Download…</button></span></td>
      <td>${w.recipe_ids.length ? w.recipe_ids.map((id) => {
        const r = state.recipes.find((x) => x.id === id);
        return r ? `<button class="chip small" data-recipe="${r.id}" title="Edit this recipe">${esc(r.name)}</button>` : "";
      }).join(" ") : `<span class="muted">–</span>`}</td>
      ${hosts.map((h) => `<td>${weightCellHtml(w, w.hosts.find((c) => c.host_id === h.id) || { host_id: h.id, state: "unset" })}</td>`).join("")}
    </tr>`).join("");
}

$("#weights").addEventListener("click", (ev) => {
  const cp = ev.target.closest("[data-copy-weight]");
  if (cp) {
    const w = weightsData.weights.find((x) => x.id === Number(cp.dataset.copyWeight));
    openTransfer({ kind: "copy", weight_id: w.id, label: w.name, sources: w.hosts.filter((c) => c.state === "present").map((c) => c.host_id) },
      w.hosts.filter((c) => c.state !== "present").map((c) => c.host_id));
    return;
  }
  const dl = ev.target.closest("[data-download-weight]");
  if (dl) {
    const w = weightsData.weights.find((x) => x.id === Number(dl.dataset.downloadWeight));
    const path = w.hosts.map((c) => c.path).find(Boolean) || "";
    const folder = path.replace(/\/+$/, "").split("/").pop() || (/^[\w.-]+$/.test(w.name) ? w.name : "");
    openTransfer({ kind: "download", repo: w.source, name: folder, weight_id: w.id },
      w.hosts.filter((c) => c.state !== "present").map((c) => c.host_id));
    return;
  }
  const r = ev.target.closest("[data-recipe]");
  if (r) { const rec = state.recipes.find((x) => x.id === Number(r.dataset.recipe)); if (rec) openRecipeEditor(rec); return; }
  const b = ev.target.closest("[data-weight]");
  if (b) openWeightEditor(state.weights.find((w) => w.id === Number(b.dataset.weight)), Number(b.dataset.host) || null);
});
$("#btn-new-weight").addEventListener("click", () => openWeightEditor(null));

let discoverData = null;

async function discoverWeights() {
  const box = $("#weights-discover");
  const btn = $("#btn-discover-weights");
  box.classList.remove("hidden");
  box.innerHTML = `<div class="muted">Searching the model folders on every host…</div>`;
  btn.disabled = true;
  try {
    discoverData = await api("/api/weights/discover");
    renderDiscover();
  } catch (e) {
    box.innerHTML = `<div class="bad">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderDiscover() {
  const d = discoverData;
  const box = $("#weights-discover");
  if (!d) { box.classList.add("hidden"); return; }
  const host = (id) => esc(maskAddr(hostName(id)));
  const size = (f) => f.size_gb ? ` <span class="muted">${f.size_gb} GB</span>` : "";
  const hostsLine = d.hosts.map((h) => h.error
    ? `<span class="bad" title="${esc(blurHostText(h.error, hostById(h.host_id)?.url))}">${host(h.host_id)}: ✗ ${esc(blurHostText(h.error, hostById(h.host_id)?.url))}</span>`
    : `<span>${host(h.host_id)}: ${h.count} found in <code>${esc(h.dirs.join(", "))}</code></span>`).join(" · ");
  const sugg = d.suggestions.length ? `<h4>Suggested paths <button class="btn small primary" data-use-all>Use all ${d.suggestions.length}</button></h4>
      ${d.suggestions.map((s, i) => `<div class="discover-row"><b>${esc(s.name)}</b> on ${host(s.host_id)}: <code>${esc(s.path)}</code>${size(s)}
        <button class="btn small" data-use="${i}">Use</button></div>`).join("")}` : "";
  const unreg = d.unregistered.length ? `<h4>Found, not registered</h4>
      ${d.unregistered.map((f, i) => `<div class="discover-row">${f.kind === "file" ? "📄" : "📁"} <b>${esc(f.name)}</b> on ${host(f.host_id)}: <code>${esc(f.path)}</code>${size(f)}
        <button class="btn small" data-register="${i}">Register…</button></div>`).join("")}` : "";
  box.innerHTML = `<div class="discover-head"><span class="muted">${hostsLine}</span><button type="button" class="x" data-close-discover title="Close">&times;</button></div>
    ${sugg}${unreg}${!sugg && !unreg ? `<div class="muted">Nothing new: every folder found is registered and every entry has its paths. Folders are looked for in each host's <code>DGXM_MODEL_DIR</code>.</div>` : ""}`;
}

const normName = (n) => n.toLowerCase().replace(/[^a-z0-9]/g, "");

$("#btn-discover-weights").addEventListener("click", discoverWeights);
$("#weights-discover").addEventListener("click", async (ev) => {
  if (ev.target.closest("[data-close-discover]")) { discoverData = null; renderDiscover(); return; }
  const use = ev.target.closest("[data-use]");
  const all = ev.target.closest("[data-use-all]");
  if (use || all) {
    const list = all ? discoverData.suggestions : [discoverData.suggestions[Number(use.dataset.use)]];
    try {
      for (const s of list) await api(`/api/weights/${s.weight_id}/hosts/${s.host_id}`, { method: "PUT", body: { path: s.path } });
      toast(`${list.length} path${list.length === 1 ? "" : "s"} set`, "ok");
      await afterWeightsChange();
    } catch (e) { toast(e.message, "bad"); }
    return;
  }
  const reg = ev.target.closest("[data-register]");
  if (reg) {
    const f = discoverData.unregistered[Number(reg.dataset.register)];
    // The same folder name on other hosts is most likely the same weights: fill those paths in too.
    const locations = {};
    for (const g of discoverData.unregistered) {
      if (normName(g.name) === normName(f.name) && !(g.host_id in locations)) locations[g.host_id] = g.path;
    }
    locations[f.host_id] = f.path;
    openWeightEditor(null, null, { name: f.name.replace(/\.gguf$/i, ""), locations });
  }
});

let editingWeight = null;

function openWeightEditor(w, focusHostId = null, prefill = null) {
  editingWeight = w || null;
  const form = $("#form-weight");
  form.reset();
  $("#weight-error").textContent = "";
  $("#weight-title").textContent = w ? `Weights – ${w.name}` : "Register weights";
  form.elements.name.value = w?.name ?? prefill?.name ?? "";
  form.elements.source.value = w?.source ?? "";
  $("#weight-paths").innerHTML = state.hosts.map((h) => `<label class="weight-path">
      <span>${esc(maskAddr(h.name))}${h.is_local ? " <small>this machine</small>" : ""}</span>
      <input class="mono" data-host="${h.id}" value="${esc(w?.locations[h.id] ?? prefill?.locations?.[h.id] ?? "")}" placeholder="~/models/…"></label>`).join("");
  $("#btn-weight-delete").classList.toggle("hidden", !w);
  $("#dlg-weight").showModal();
  const focus = focusHostId && $(`#weight-paths input[data-host="${focusHostId}"]`);
  (focus || form.elements.name).focus();
}

async function afterWeightsChange() {
  await loadWeightsList();
  if (state.tab === "models") loadWeights();
  if (discoverData) discoverWeights();  // the search results above the registry are stale now
  if ($("#dlg-recipe").open) updateWeightsStatus();
  renderRecipes();
}

$("#form-weight").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const locations = Object.fromEntries($$("#weight-paths input").map((i) => [i.dataset.host, i.value.trim()]).filter(([, v]) => v));
  const body = { name: form.elements.name.value, source: form.elements.source.value, locations };
  try {
    const saved = editingWeight
      ? await api(`/api/weights/${editingWeight.id}`, { method: "PUT", body })
      : await api("/api/weights", { method: "POST", body });
    $("#dlg-weight").close();
    toast("Weights saved", "ok");
    await afterWeightsChange();
    // Registered from the recipe editor: pick the new entry there right away.
    if (!editingWeight && $("#dlg-recipe").open && !recipeForm.elements.weights_id.value) {
      recipeForm.elements.weights_id.value = String(saved.id);
      $("#sel-weights").dispatchEvent(new Event("change"));
    }
  } catch (e) {
    $("#weight-error").textContent = e.message;
  }
});

$("#btn-weight-delete").addEventListener("click", async () => {
  if (!editingWeight || !confirm(`Delete the weights entry “${editingWeight.name}”? The files stay where they are.`)) return;
  try {
    await api(`/api/weights/${editingWeight.id}`, { method: "DELETE" });
    $("#dlg-weight").close();
    toast("Weights entry deleted", "ok");
    await afterWeightsChange();
  } catch (e) {
    $("#weight-error").textContent = e.message;
  }
});

/* ---------------- import ---------------- */

function showRecipeNote(lines) {
  const note = $("#recipe-note");
  note.textContent = lines.join("\n");
  note.classList.toggle("hidden", !lines.length);
}

function resetImportReport() {
  $("#import-report").classList.add("hidden");
  $("#import-trust-row").classList.add("hidden");
  $("#import-trust").checked = false;
  $("#import-error").textContent = "";
}

function renderImportReport(check) {
  const box = $("#import-report");
  box.classList.remove("hidden");
  box.innerHTML = `<div class="report-head ${check.count ? "bad" : "ok"}">${check.count
      ? `${check.count} risk${check.count === 1 ? "" : "s"} found`
      : "No risky calls found."}</div>` +
    check.findings.map((f) => `<div class="finding">
        <span class="ln">line ${f.line}</span>
        <span class="msg">${esc(f.message)}</span>
        <code>${esc(f.code)}</code>
      </div>`).join("");
  $("#import-trust-row").classList.toggle("hidden", !check.count);
}

async function runImportCheck() {
  const check = await api("/api/recipes/import/check", { method: "POST", body: { text: $("#import-text").value } });
  renderImportReport(check);
  return check;
}

function openImport() {
  resetImportReport();
  $("#import-alternatives").classList.toggle("hidden", !!editingRecipe);
  $("#dlg-import").showModal();
  $("#import-text").focus();
}
$("#btn-import").addEventListener("click", openImport);
$("#import-cta [data-import]").addEventListener("click", openImport);
$("#btn-import-manual").addEventListener("click", () => $("#dlg-import").close());
$("#btn-import-cancel").addEventListener("click", () => {
  if (!editingRecipe && $("#dlg-recipe").open) $("#dlg-recipe").close();
});
$("#btn-import-quick").addEventListener("click", () => {
  $("#dlg-import").close();
  if ($("#dlg-recipe").open) $("#dlg-recipe").close();
  openQuick();
});

$("#import-text").addEventListener("input", resetImportReport);

$("#btn-import-check").addEventListener("click", async () => {
  try { await runImportCheck(); } catch (e) { $("#import-error").textContent = e.message; }
});

$("#form-import").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#btn-import-go");
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const check = await runImportCheck();
    const trust = $("#import-trust").checked;
    if (check.count && !trust) {
      $("#import-error").textContent = "Review the risks above and tick the box to import anyway.";
      return;
    }
    btn.textContent = "Importing…";
    const res = await api("/api/recipes/import", { method: "POST", body: { text: $("#import-text").value, trust } });
    const r = res.recipe;
    if (editingRecipe) {
      // Re-importing into an existing recipe keeps its identity.
      r.name = editingRecipe.name;
      r.container_name = editingRecipe.container_name;
      r.description = r.description || editingRecipe.description;
      r.est_memory_gb = editingRecipe.est_memory_gb;
      r.color = editingRecipe.color;
    }
    // Scripts carry no tags; keep whatever the form already has.
    if (!r.tags?.length) r.tags = recipeFromForm().tags;
    fillRecipeForm(r);
    const adopted = useRegisteredWeights();
    syncHostNetworkPorts();
    updateWeightsStatus();
    scheduleImageStatus();
    const skipped = res.skipped_calls || [];
    const riskCount = res.check?.count ?? 0;
    showRecipeNote([
      "Imported – review the fields and the command preview, then save.",
      ...(adopted ? [`• The model folder is the registered weights “${adopted}”: the volume now uses {weights}.`] : []),
      ...res.warnings.map((w) => `• ${w}`),
      ...(riskCount ? [`• Script check: ${riskCount} risk${riskCount === 1 ? "" : "s"} (confirmed)`] : []),
      ...(skipped.length ? [`• Not executed during import (${skipped.length}):`,
        ...skipped.slice(0, 12).map((s) => `    ${s}`), ...(skipped.length > 12 ? [`    … ${skipped.length - 12} more`] : [])] : []),
    ]);
    $("#import-cta").classList.add("hidden");
    $("#dlg-import").close();
    updatePreview();
  } catch (e) {
    $("#import-error").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Import";
  }
});

/* ---------------- copies on other hosts ---------------- */

async function syncCopies(id) {
  try {
    const res = await api(`/api/recipes/${id}/sync`, { method: "POST" });
    toast(`${res.updated.length} cop${res.updated.length === 1 ? "y" : "ies"} updated`, "ok");
    await loadRecipes();
    const running = res.running.map((rid) => state.recipes.find((x) => x.id === rid)).filter(Boolean);
    if (running.length && confirm(`Running with the old settings: ${running.map((x) => `“${x.name}”`).join(", ")}.\n\nRestart ${running.length === 1 ? "it" : "them"} now?`)) {
      await startRestartJob(running.map((x) => x.id), "Restarting copies");
    }
  } catch (e) { toast(e.message, "bad"); }
}

/* Background jobs: they run on the hub (closing the browser does not stop them); this only shows them. */

const jobs = new Map();
let jobTimer = null;
const JOB_ICON = { waiting: `<span class="muted" title="Waiting for this host">⏸</span>`, running: "⏳",
  ok: `<span class="ok">✓</span>`, failed: `<span class="bad">✗</span>` };
const JOB_DONE = { restart: "started", pull: "pulled", download: "done", copy: "copied" };

function jobItemState(j, i) {
  if (i.state === "failed") return "failed";
  if (i.state === "waiting") return "waiting";
  if (i.state === "running") return j.kind === "restart" ? "restarting…" : esc(blurText(i.progress || "running…")).slice(0, 60);
  if (i.message === "already there") return "already there";
  const size = (i.progress || "").match(/([\d.]+) GB(?!.*GB)/)?.[1];
  return `${JOB_DONE[j.kind] ?? "done"}${(j.kind === "download" || j.kind === "copy") && size ? ` · ${size} GB` : ""}`;
}

// A job that changes what hosts have: refresh what the open tab shows once it is done.
function afterJob(j) {
  loadRecipes();
  if (j.kind === "download" || j.kind === "copy") afterWeightsChange();
  if (j.kind === "pull" && state.tab === "images") loadImages();
}

function showJob(job) {
  jobs.set(job.id, job);
  renderJobs();
  pollJobs();
}

async function startRestartJob(ids, title) {
  try {
    showJob(await api("/api/jobs/restart", { method: "POST", body: { recipe_ids: ids, title } }));
  } catch (e) { toast(e.message, "bad"); }
}

/* Pull an image / download weights on several hosts: each host does it itself, the panel shows progress. */

let transferSpec = null;

// spec: { kind: "pull", image } or { kind: "download", repo, name, weight_id }; `missing` = host ids to tick.
function openTransfer(spec, missing = []) {
  transferSpec = spec;
  const form = $("#form-transfer");
  form.reset();
  $("#transfer-error").textContent = "";
  const pull = spec.kind === "pull", copy = spec.kind === "copy";
  $("#transfer-title").textContent = pull ? "Pull on hosts" : copy ? `Copy “${spec.label}” to hosts` : "Download on hosts";
  $("#transfer-hint").innerHTML = pull
    ? `Each host runs <code>docker pull ${esc(spec.image)}</code> itself (private registries: <code>docker login</code> there once).`
    : copy ? `Copied host to host over your network – the hub only arranges it. Every host that has the weights serves
        one copy at a time and each finished host joins in, so many hosts are done in a few rounds. Each file is checked
        with SHA-256; files a host already has are skipped, and an interrupted copy continues where it stopped.`
    : `Downloads from Hugging Face (needs <code>hf</code> on the host: <code>pip install -U huggingface_hub</code>; gated
        models: <code>HF_TOKEN</code> in its <code>.env</code>). The folders are registered in the Weights tab.`;
  $$(".hf-only", form).forEach((el) => el.classList.toggle("hidden", pull || copy));
  form.elements.once.checked = true;
  form.elements.repo.value = spec.repo ?? "";
  form.elements.name.value = spec.name ?? "";
  $("#transfer-hosts").innerHTML = state.hosts.map((h) => `<label class="copy-host">
      <input type="checkbox" value="${h.id}" ${missing.includes(h.id) ? "checked" : ""} ${h.online ? "" : "disabled"}>
      <span class="copy-host-name">${esc(maskAddr(h.name))}</span>
      <span class="copy-host-check muted">${!h.online ? "offline" : missing.includes(h.id) ? "missing here" : copy ? "has it" : "has it already / not checked"}
        ${transportBadge(h)}</span></label>`).join("");
  updateTransferWarning();
  $("#dlg-transfer").showModal();
}

// Copies (and "download once") send weights between machines: warn when that is not encrypted.
function updateTransferWarning() {
  const box = $("#transfer-warning");
  const spec = transferSpec;
  const ticked = $$("#transfer-hosts input:checked").map((i) => Number(i.value));
  const once = spec.kind === "download" && $("#form-transfer").elements.once.checked && ticked.length > 1;
  const involved = spec.kind === "copy" ? [...ticked, ...(spec.sources || [])] : once ? ticked : [];
  const plain = plainHosts(involved);
  box.classList.toggle("hidden", !plain.length);
  box.innerHTML = plain.length ? `⚠ Not encrypted: ${plain.map((h) => `<b>${esc(maskAddr(h.name))}</b>`).join(", ")}
    ${plain.length === 1 ? "is" : "are"} reached over plain HTTP, so the weights travel readable on the network (the
    SHA-256 check still catches damaged or swapped files). Use Tailscale names (or HTTPS) as host addresses to encrypt it.` : "";
  $("#transfer-once-row").classList.toggle("hidden", spec.kind !== "download");
}
$("#transfer-hosts").addEventListener("change", updateTransferWarning);
$("#form-transfer").elements.once.addEventListener("change", updateTransferWarning);

$("#form-transfer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  const host_ids = $$("#transfer-hosts input:checked").map((i) => Number(i.value));
  if (!host_ids.length) { $("#transfer-error").textContent = "Pick at least one host."; return; }
  try {
    const job = transferSpec.kind === "pull"
      ? await api("/api/jobs/pull", { method: "POST", body: { image: transferSpec.image, host_ids } })
      : transferSpec.kind === "copy"
      ? await api("/api/jobs/copy", { method: "POST", body: { weight_id: transferSpec.weight_id, host_ids } })
      : await api("/api/jobs/download", { method: "POST", body: { repo: f.repo.value.trim(), name: f.name.value.trim(),
        host_ids, weight_id: transferSpec.weight_id ?? null, once: f.once.checked } });
    $("#dlg-transfer").close();
    showJob(job);
  } catch (e) {
    $("#transfer-error").textContent = e.message;
  }
});

$("#btn-download-weights").addEventListener("click", () =>
  openTransfer({ kind: "download" }, state.hosts.filter((h) => h.online).map((h) => h.id)));

function renderJobs() {
  $("#jobs").innerHTML = [...jobs.values()].map((j) => {
    const done = j.items.filter((i) => i.state === "ok" || i.state === "failed").length;
    return `<div class="job" data-job="${esc(j.id)}">
      <div class="job-head">${j.done ? (j.items.some((i) => i.state === "failed") ? `<span class="bad">✗</span>` : `<span class="ok">✓</span>`) : "⏳"}
        ${esc(j.title)} <span class="muted">${done} / ${j.items.length}</span>
        ${j.done ? `<button type="button" class="x" data-close-job="${esc(j.id)}" title="Close">&times;</button>` : ""}</div>
      ${j.items.map((i) => `<div class="job-item">${JOB_ICON[i.state] ?? ""}
        <span>${i.name ? `${esc(recipeTitle({ ...i, copy_of: 1 }))}${multiHost() ? ` <span class="muted">⌂ ${esc(maskAddr(hostName(i.host_id)))}</span>` : ""}`
          : `⌂ ${esc(maskAddr(hostName(i.host_id)))}`}</span>
        <span class="muted">${jobItemState(j, i)}</span>
        ${i.state === "failed" ? `<span class="job-msg bad">${esc(blurText(i.message))}</span>` : ""}</div>`).join("")}
    </div>`;
  }).join("");
}

async function pollJobs() {
  clearTimeout(jobTimer);
  const open = [...jobs.values()].filter((j) => !j.done);
  if (!open.length) return;
  for (const j of open) {
    try {
      const fresh = await api(`/api/jobs/${j.id}`);
      jobs.set(j.id, fresh);
      if (fresh.done) {
        const failed = fresh.items.filter((i) => i.state === "failed").length;
        toast(`${fresh.title}: ${failed ? `${failed} failed` : `all ${JOB_DONE[fresh.kind] ?? "done"}`}`, failed ? "bad" : "ok");
        afterJob(fresh);
        // Successful jobs close themselves; failed ones stay until closed, so the reason can be read.
        if (!failed) setTimeout(() => { jobs.delete(fresh.id); renderJobs(); }, 30000);
      }
    } catch { jobs.delete(j.id); }  // the manager restarted: the job is gone
  }
  renderJobs();
  jobTimer = setTimeout(pollJobs, 1500);
}

$("#jobs").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-close-job]");
  if (b) { jobs.delete(b.dataset.closeJob); renderJobs(); }
});

// After a reload: show jobs that are still running on the hub.
async function resumeJobs() {
  try {
    for (const j of await api("/api/jobs")) if (!j.done) jobs.set(j.id, j);
  } catch { return; }
  renderJobs();
  pollJobs();
}

let copyFrom = null;

async function openCopyTo(r) {
  copyFrom = r.copy_of ? state.recipes.find((x) => x.id === r.copy_of) || r : r;
  $("#copy-title").textContent = `Copy “${copyFrom.name}” to other hosts`;
  $("#copy-error").textContent = "";
  const hosts = state.hosts.filter((h) => h.id !== copyFrom.host_id);
  const box = $("#copy-hosts");
  box.innerHTML = hosts.map((h) => `<label class="copy-host"><input type="checkbox" value="${h.id}">
      <span class="copy-host-name">${esc(maskAddr(h.name))}${h.online ? "" : " <small>(offline)</small>"}</span>
      <span class="copy-host-check muted" data-check="${h.id}">checking…</span></label>`).join("")
    || `<div class="muted">No other hosts: add them in the Hosts tab.</div>`;
  $("#dlg-copy").showModal();
  if (!hosts.length) return;
  try {
    const res = await api(`/api/recipes/${copyFrom.id}/copies/check`, { method: "POST", body: { host_ids: hosts.map((h) => h.id) } });
    for (const c of res.hosts) {
      const bits = [];
      if (c.existing_copy) bits.push(`<span class="ok">has a copy – will be updated</span>`);
      const w = c.weights;
      if (w) bits.push(w.state === "present" ? `<span class="ok">✓ weights</span>`
        : w.state === "unset" ? `<span class="bad">✗ weights: no path – set it in the Weights tab</span>`
        : w.state === "missing" ? `<span class="bad">✗ weights not at ${esc(w.location || w.path)}</span>` : `<span class="muted">? weights</span>`);
      if (c.image) bits.push(c.image === "present" ? `<span class="ok">✓ image</span>`
        : c.image === "missing" ? `<span class="warn">image not there yet – pulled on first start</span>` : `<span class="muted">? image</span>`);
      if (c.port_users.length) bits.push(`<span class="warn">port ${copyFrom.host_port} also used by ${esc(c.port_users.join(", "))}</span>`);
      if (c.error) bits.push(`<span class="muted">${esc(blurHostText(c.error, hostById(c.host_id)?.url))}</span>`);
      const cell = box.querySelector(`[data-check="${c.host_id}"]`);
      if (cell) cell.innerHTML = bits.join(" · ") || `<span class="ok">✓ ready</span>`;
      if (cell && c.existing_copy) cell.closest("label").querySelector("input").checked = true;
    }
  } catch (e) {
    $("#copy-error").textContent = e.message;
  }
}

$("#form-copy").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ids = $$("#copy-hosts input:checked").map((i) => Number(i.value));
  if (!ids.length) { $("#copy-error").textContent = "Pick at least one host."; return; }
  try {
    const res = await api(`/api/recipes/${copyFrom.id}/copies`, { method: "POST", body: { host_ids: ids } });
    $("#dlg-copy").close();
    toast(`${res.created.length} cop${res.created.length === 1 ? "y" : "ies"} created${res.updated.length ? `, ${res.updated.length} updated` : ""}`, "ok");
    await loadRecipes();
  } catch (e) {
    $("#copy-error").textContent = e.message;
  }
});

/* ---------------- quick recipe ---------------- */

// Weights on the host + the settings of a recipe that already works (or a preset) = a new recipe.
const quickForm = $("#form-quick");
let quickFound = [];      // model folders / .gguf files on the hosts nobody registered yet
let quickBaseTouched = false;
let quickModelTouched = false;
let quickTimer = null;
let quickSeq = 0;

const isGguf = (ref) => /\.gguf$/i.test(ref);

function quickModelRef() {
  const v = quickForm.elements.model_ref.value;
  if (v !== "other") return v;
  const t = quickForm.elements.other.value.trim();
  return !t ? "" : /^[/~]/.test(t) ? `f:${t}` : `h:${t}`;
}

function renderQuickModels() {
  const hostId = Number(quickForm.elements.host_id.value);
  const cur = quickForm.elements.model_ref.value;
  const reg = state.weights.filter((w) => w.locations[hostId]);
  const found = quickFound.filter((f) => f.host_id === hostId);
  const size = (f) => f.size_gb ? ` – ${f.size_gb} GB` : "";
  quickForm.elements.model_ref.innerHTML = [
    reg.length ? `<optgroup label="Registered weights">${reg.map((w) =>
      `<option value="w:${w.id}">${esc(w.name)}</option>`).join("")}</optgroup>` : "",
    found.length ? `<optgroup label="Found on this host">${found.map((f) =>
      `<option value="f:${esc(f.path)}">${f.kind === "file" ? "📄" : "📁"} ${esc(f.name)}${size(f)}</option>`).join("")}</optgroup>` : "",
    `<optgroup label="Other"><option value="other">Hugging Face id or another path…</option></optgroup>`,
  ].join("");
  const keep = quickModelTouched && [...quickForm.elements.model_ref.options].some((o) => o.value === cur);
  quickForm.elements.model_ref.value = keep ? cur : quickForm.elements.model_ref.options[0].value;
  $("#quick-other-row").classList.toggle("hidden", quickForm.elements.model_ref.value !== "other");
}

function renderQuickBases() {
  const hostId = Number(quickForm.elements.host_id.value);
  const cur = quickForm.elements.base.value;
  const models = state.recipes.filter((r) => kindOf(r) === "model").sort(byName);
  const opt = (r) => `<option value="r:${r.id}">${esc(r.name)} (${esc(state.meta.frameworks[r.framework]?.label ?? r.framework)})</option>`;
  const here = models.filter((r) => r.host_id === hostId);
  const elsewhere = models.filter((r) => r.host_id !== hostId);
  const mac = hostPlatform(hostId) === "darwin";
  const presets = Object.entries(state.meta.frameworks)
    .filter(([k, v]) => !k.endsWith("custom") && (!mac || v.runtime === "process"));
  quickForm.elements.base.innerHTML = [
    here.length ? `<optgroup label="Recipes on this host">${here.map(opt).join("")}</optgroup>` : "",
    elsewhere.length ? `<optgroup label="Recipes on other hosts">${elsewhere.map(opt).join("")}</optgroup>` : "",
    `<optgroup label="Engine defaults">${presets.map(([k, v]) => `<option value="p:${k}">${esc(v.label)}</option>`).join("")}</optgroup>`,
  ].join("");
  if ([...quickForm.elements.base.options].some((o) => o.value === cur)) quickForm.elements.base.value = cur;
}

// The settings that most likely fit: a recipe on this host for the same kind of weights, else a preset.
function suggestQuickBase() {
  if (quickBaseTouched) return;
  const hostId = Number(quickForm.elements.host_id.value);
  const gguf = isGguf(quickModelRef());
  const mac = hostPlatform(hostId) === "darwin";
  const llama = (fw) => fw.startsWith("llamacpp");
  const fits = (r) => kindOf(r) === "model" && r.host_id === hostId && (gguf ? llama(r.framework)
    : !llama(r.framework) && (!mac || r.runtime === "process"));
  const recent = state.recipes.filter(fits).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""))[0];
  quickForm.elements.base.value = recent ? `r:${recent.id}`
    : `p:${gguf ? (mac ? "llamacpp-native" : "llamacpp") : mac ? "mlx" : "vllm"}`;
}

function quickBody(save) {
  const f = quickForm.elements;
  const ref = quickModelRef();
  const base = f.base.value;
  const body = { host_id: Number(f.host_id.value) || null, name: f.name.value.trim(), save,
    context: f.context.value ? Number(f.context.value) : null };
  if (base.startsWith("r:")) body.base_recipe_id = Number(base.slice(2));
  else body.framework = base.slice(2);
  if (ref.startsWith("w:")) body.weights_id = Number(ref.slice(2));
  else if (ref.startsWith("f:")) body.path = ref.slice(2);
  else if (ref.startsWith("h:")) body.model = ref.slice(2);
  return body;
}

async function updateQuickPreview() {
  const seq = ++quickSeq;
  $("#quick-error").textContent = "";
  if (!quickModelRef()) {
    $("#quick-preview").textContent = "Pick the model first.";
    $("#quick-notes").innerHTML = "";
    return;
  }
  try {
    const res = await api("/api/recipes/quick", { method: "POST", body: quickBody(false) });
    if (seq !== quickSeq) return;
    const r = res.recipe;
    $("#quick-notes").innerHTML = [
      `<b>${esc(r.name)}</b> on port <b>${r.host_port}</b> – endpoint <code>${esc(maskEndpoint(endpointUrl(r)))}</code>`,
      ...res.notes.map(esc),
    ].map((n) => `<li>${n}</li>`).join("");
    $("#quick-preview").innerHTML = previewHtml(res.command, r.image);
  } catch (e) {
    if (seq !== quickSeq) return;
    $("#quick-notes").innerHTML = "";
    $("#quick-preview").textContent = `(incomplete) ${e.message}`;
  }
}

function scheduleQuickPreview() {
  clearTimeout(quickTimer);
  quickTimer = setTimeout(updateQuickPreview, 250);
}

async function openQuick() {
  quickForm.reset();
  quickBaseTouched = false;
  quickModelTouched = false;
  $("#quick-error").textContent = "";
  $("#quick-host").innerHTML = state.hosts.map((h) =>
    `<option value="${h.id}">${esc(maskAddr(h.name))}${h.online ? "" : " (offline)"}</option>`).join("");
  $("#quick-host-row").classList.toggle("hidden", !multiHost());
  quickForm.elements.host_id.value = view.host || state.hosts.find((h) => h.is_local)?.id || "";
  renderQuickModels();
  renderQuickBases();
  suggestQuickBase();
  $("#dlg-quick").showModal();
  updateQuickPreview();
  // Unregistered folders take a round trip to every host: fill them in when they arrive.
  try {
    quickFound = (await api("/api/weights/discover")).unregistered;
    if (!$("#dlg-quick").open) return;
    renderQuickModels();
    suggestQuickBase();
    updateQuickPreview();
  } catch { /* the registered weights and "Other" still work */ }
}

$("#quick-host").addEventListener("change", () => {
  renderQuickModels();
  renderQuickBases();
  suggestQuickBase();
  updateQuickPreview();
});
$("#quick-model").addEventListener("change", () => {
  quickModelTouched = true;
  $("#quick-other-row").classList.toggle("hidden", quickForm.elements.model_ref.value !== "other");
  if (quickForm.elements.model_ref.value === "other") quickForm.elements.other.focus();
  suggestQuickBase();
  updateQuickPreview();
});
$("#quick-base").addEventListener("change", () => { quickBaseTouched = true; updateQuickPreview(); });
quickForm.addEventListener("input", (ev) => {
  if (ev.target.name === "other") suggestQuickBase();
  if (["other", "context", "name"].includes(ev.target.name)) scheduleQuickPreview();
});

async function createQuick(edit) {
  try {
    const res = await api("/api/recipes/quick", { method: "POST", body: quickBody(true) });
    $("#dlg-quick").close();
    toast(`“${res.recipe.name}” created${edit ? "" : " – press Start on its card"}`, "ok");
    await loadWeightsList();
    await loadRecipes();
    if (edit) openRecipeEditor(state.recipes.find((r) => r.id === res.recipe.id) || res.recipe);
  } catch (e) {
    $("#quick-error").textContent = e.message;
  }
}

quickForm.addEventListener("submit", (ev) => { ev.preventDefault(); createQuick(false); });
$("#btn-quick-edit").addEventListener("click", () => createQuick(true));

/* ---------------- logs ---------------- */

let logSource = null;
let logLines = [];
const MAX_LOG_LINES = 20000;

function logClass(line) {
  if (/\b(ERROR|CRITICAL|Traceback|Exception)\b/.test(line)) return "err";
  if (/\bWARN(ING)?\b/.test(line)) return "warn";
  return "";
}

function renderLogs() {
  const filter = $("#logs-filter").value.toLowerCase();
  const lines = filter ? logLines.filter((l) => l.toLowerCase().includes(filter)) : logLines;
  $("#logs").innerHTML = lines.map((l) => {
    const c = logClass(l);
    return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
  }).join("\n");
  if ($("#logs-follow").checked) $("#logs").scrollTop = $("#logs").scrollHeight;
}

let renderPending = false;
function scheduleLogRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; renderLogs(); });
}

function openLogs(r) {
  closeLogStream();
  logLines = [];
  $("#logs-title").textContent = `Logs – ${r.name}`;
  $("#logs").textContent = "";
  $("#logs-state").textContent = "connecting";
  $("#logs-state").className = "pill loading";
  $("#dlg-logs").showModal();
  $("#dlg-logs").dataset.name = r.container_name;
  logSource = new EventSource(`/api/recipes/${r.id}/logs/stream?tail=1000`);
  logSource.onopen = () => { $("#logs-state").textContent = "live"; $("#logs-state").className = "pill ready"; };
  logSource.onmessage = (e) => {
    logLines.push(e.data);
    if (logLines.length > MAX_LOG_LINES) logLines.splice(0, logLines.length - MAX_LOG_LINES);
    scheduleLogRender();
  };
  logSource.addEventListener("end", () => {
    $("#logs-state").textContent = "container stopped";
    $("#logs-state").className = "pill";
    closeLogStream();
  });
  logSource.onerror = () => {
    if (!logSource) return;
    $("#logs-state").textContent = "disconnected";
    $("#logs-state").className = "pill failed";
    closeLogStream();
  };
}

function closeLogStream() {
  if (logSource) { logSource.close(); logSource = null; }
}

$("#dlg-logs").addEventListener("close", closeLogStream);
$("#logs-filter").addEventListener("input", renderLogs);
$("#logs-clear").addEventListener("click", () => { logLines = []; renderLogs(); });
$("#logs-download").addEventListener("click", () => {
  const blob = new Blob([logLines.join("\n")], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${$("#dlg-logs").dataset.name || "model"}.log`;
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ---------------- schedules ---------------- */

const scheduleForm = $("#form-schedule");
let editingSchedule = null;

$("#days").innerHTML = DAYS.map((d, i) => `<label><input type="checkbox" value="${i}" checked> ${d}</label>`).join("");

function describeCron(cron) {
  const m = /^(\d{1,2}) (\d{1,2}) \* \* ([\d,*-]+)$/.exec(cron || "");
  if (!m) return null;
  const time = `${m[2].padStart(2, "0")}:${m[1].padStart(2, "0")}`;
  let days;
  if (m[3] === "*") days = [0, 1, 2, 3, 4, 5, 6];
  else {
    days = [];
    for (const part of m[3].split(",")) {
      const [a, b] = part.split("-").map(Number);
      if (b === undefined) days.push(a % 7);
      else for (let i = a; i <= b; i++) days.push(i % 7);
    }
  }
  return { time, days: [...new Set(days)].sort() };
}

function cronLabel(cron) {
  const d = describeCron(cron);
  if (!d) return `cron ${cron}`;
  const days = d.days.length === 7 ? "every day" : d.days.join() === "1,2,3,4,5" ? "weekdays" : d.days.map((i) => DAYS[i]).join(", ");
  return `${days} at ${d.time}`;
}

function actionLabel(s) {
  const a = state.meta.actions[s.action]?.label ?? s.action;
  if (!s.recipe_id) return multiHost() ? `${a} – ${s.host_id ? maskAddr(hostName(s.host_id)) : "all hosts"}` : a;
  const r = state.recipes.find((x) => x.id === s.recipe_id);
  const where = multiHost() && r ? ` (${maskAddr(hostName(r.host_id))})` : "";
  return `${a.split(" (")[0]}: ${r ? r.name : `#${s.recipe_id}`}${where}`;
}

async function loadSchedules() {
  try {
    state.schedules = await api("/api/schedules");
  } catch (e) { toast(e.message, "bad"); return; }
  try { state.schedPaused = (await api("/api/settings")).schedules_paused; } catch { /* keep last */ }
  renderSchedPause();
  loadTimeline();
  const tbody = $("#schedules");
  if (!state.schedules.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No schedules yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.schedules.map((s) => `
    <tr data-id="${s.id}">
      <td><input type="checkbox" data-act="toggle" ${s.enabled ? "checked" : ""}></td>
      <td>${esc(s.name)}</td>
      <td>${esc(actionLabel(s))}</td>
      <td>${esc(s.cron ? cronLabel(s.cron) : `once ${fmtTime(s.run_at)}`)}</td>
      <td>${s.enabled ? esc(fmtTime(s.next_run)) : "–"}</td>
      <td class="msg">${s.last_run ? `<span class="${s.last_ok ? "ok" : "bad"}">${s.last_ok ? "✓" : "✗"}</span> ${esc(fmtTime(s.last_run))}<br><small>${esc(s.last_message || "")}</small>` : "–"}</td>
      <td style="white-space:nowrap">
        <button class="btn small ghost" data-act="run">Run now</button>
        <button class="btn small ghost" data-act="duplicate">Duplicate</button>
        <button class="btn small ghost" data-act="edit">Edit</button>
        <button class="btn small ghost danger" data-act="delete">Delete</button>
      </td>
    </tr>`).join("");
}

function scheduleBody(s, overrides = {}) {
  return { name: s.name, action: s.action, recipe_id: s.recipe_id, cron: s.cron, run_at: s.run_at, enabled: !!s.enabled,
           host_id: s.host_id, ...overrides };
}

$("#schedules").addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const id = Number(el.closest("tr").dataset.id);
  const s = state.schedules.find((x) => x.id === id);
  try {
    if (el.dataset.act === "toggle") {
      await api(`/api/schedules/${id}`, { method: "PUT", body: scheduleBody(s, { enabled: el.checked }) });
    } else if (el.dataset.act === "run") {
      if (!confirm(`Run “${s.name}” now?`)) return;
      el.disabled = true;
      const res = await api(`/api/schedules/${id}/run`, { method: "POST" });
      toast(res.message, "ok");
      loadRecipes();
    } else if (el.dataset.act === "duplicate") {
      let i = 1;
      const names = new Set(state.schedules.map((x) => x.name));
      while (names.has(`${s.name} (copy ${i})`)) i += 1;
      await api("/api/schedules", { method: "POST", body: scheduleBody(s, { name: `${s.name} (copy ${i})` }) });
      toast("Schedule duplicated — adjust and save, or just use it as is", "ok");
    } else if (el.dataset.act === "edit") {
      openScheduleEditor(s);
      return;
    } else if (el.dataset.act === "delete") {
      if (!confirm(`Delete schedule “${s.name}”?`)) return;
      await api(`/api/schedules/${id}`, { method: "DELETE" });
    }
  } catch (e) {
    toast(e.message, "bad");
  }
  loadSchedules();
});

/* Master pause for all schedules. */
function renderSchedPause() {
  const b = $("#btn-sched-pause");
  b.textContent = state.schedPaused ? "▶ Resume all" : "⏸ Pause all";
  b.classList.toggle("paused-on", !!state.schedPaused);
  b.title = state.schedPaused ? "Schedules are paused – click to let them fire again" : "Stop all schedules from firing (manual actions keep working)";
  $("#sched-paused-hint").classList.toggle("hidden", !state.schedPaused);
}
$("#btn-sched-pause").addEventListener("click", async () => {
  try {
    const res = await api("/api/settings", { method: "PUT", body: { schedules_paused: !state.schedPaused } });
    state.schedPaused = res.schedules_paused;
    toast(state.schedPaused ? "All schedules paused" : "All schedules resumed", "ok");
    renderSchedPause();
    loadSchedules();
  } catch (e) { toast(e.message, "bad"); }
});

function syncScheduleForm() {
  const needs = state.meta.actions[$("#sel-action").value]?.needs_recipe;
  $("#lbl-recipe").classList.toggle("hidden", !needs);
  $("#lbl-host").classList.toggle("hidden", needs || !multiHost());
  const when = $("#sel-when").value;
  $$(".when", scheduleForm).forEach((el) => el.classList.toggle("hidden", el.dataset.when !== when));
}

function toLocalInput(iso) {
  const d = iso ? new Date(iso) : new Date(Date.now() + 3600e3);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openScheduleEditor(s) {
  editingSchedule = s;
  scheduleForm.reset();
  $("#schedule-error").textContent = "";
  $("#schedule-title").textContent = s ? "Edit schedule" : "New schedule";
  $("#sel-recipe").innerHTML = state.recipes.map((r) =>
    `<option value="${r.id}">${kindOf(r) === "agent" ? "🤖 " : ""}${esc(r.name)}${multiHost() ? ` – ${esc(maskAddr(hostName(r.host_id)))}` : ""}</option>`).join("");
  $("#sel-sched-host").innerHTML = `<option value="">All hosts</option>` +
    state.hosts.map((h) => `<option value="${h.id}">${esc(maskAddr(h.name))}</option>`).join("");
  $("#sel-sched-host").value = s?.host_id ?? "";
  $("#in-once").value = toLocalInput(s?.run_at);
  $$("#days input").forEach((c) => { c.checked = true; });
  $("#in-time").value = "22:00";
  $("#in-cron").value = "";
  if (s) {
    scheduleForm.elements.name.value = s.name;
    scheduleForm.elements.action.value = s.action;
    if (s.recipe_id) scheduleForm.elements.recipe_id.value = s.recipe_id;
    scheduleForm.elements.enabled.checked = !!s.enabled;
    if (s.cron) {
      const d = describeCron(s.cron);
      $("#in-cron").value = s.cron;
      if (d) {
        $("#sel-when").value = "weekly";
        $("#in-time").value = d.time;
        $$("#days input").forEach((c) => { c.checked = d.days.includes(Number(c.value)); });
      } else {
        $("#sel-when").value = "cron";
      }
    } else {
      $("#sel-when").value = "once";
    }
  } else {
    $("#sel-when").value = "weekly";
  }
  syncScheduleForm();
  $("#dlg-schedule").showModal();
}

$("#btn-new-schedule").addEventListener("click", () => {
  if (!state.recipes.length) toast("Create a recipe first for start/stop/switch schedules");
  openScheduleEditor(null);
});
$("#sel-action").addEventListener("change", syncScheduleForm);
$("#sel-when").addEventListener("change", syncScheduleForm);

scheduleForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = scheduleForm.elements;
  const body = {
    name: f.name.value,
    action: f.action.value,
    recipe_id: state.meta.actions[f.action.value].needs_recipe ? Number(f.recipe_id.value) : null,
    host_id: !state.meta.actions[f.action.value].needs_recipe && f.host_id.value ? Number(f.host_id.value) : null,
    enabled: f.enabled.checked,
    cron: null,
    run_at: null,
  };
  const when = $("#sel-when").value;
  if (when === "weekly") {
    const [h, m] = $("#in-time").value.split(":").map(Number);
    const days = $$("#days input").filter((c) => c.checked).map((c) => c.value);
    if (!days.length) { $("#schedule-error").textContent = "Pick at least one day"; return; }
    body.cron = `${m} ${h} * * ${days.length === 7 ? "*" : days.join(",")}`;
  } else if (when === "cron") {
    body.cron = $("#in-cron").value;
  } else {
    body.run_at = $("#in-once").value;
  }
  try {
    if (editingSchedule) await api(`/api/schedules/${editingSchedule.id}`, { method: "PUT", body });
    else await api("/api/schedules", { method: "POST", body });
    $("#dlg-schedule").close();
    toast("Schedule saved", "ok");
    loadSchedules();
  } catch (e) {
    $("#schedule-error").textContent = e.message;
  }
});


/* ---------------- schedule timeline ---------------- */

const TL_KEY = "dgxm.timelineHours";
let tlHours = 24;
try { tlHours = Number(localStorage.getItem(TL_KEY)) || 24; } catch { /* storage unavailable */ }
let tlData = null;

const CARD_HEX = {
  "": "#4f8cff", green: "#22c58b", teal: "#2cc4c9", blue: "#4f8cff", purple: "#a77bf3", pink: "#ec6fb4",
  orange: "#f2884b", yellow: "#f0c93a", red: "#ef5b5b", black: "#000000",
};
const ACTION_SHORT = { start: "start", stop: "stop", restart: "restart", switch: "switch to", stop_all: "stop all", free_memory: "free memory" };
const HOUR = 3600e3;

async function loadTimeline() {
  $$("#tl-range button").forEach((b) => b.classList.toggle("on", Number(b.dataset.h) === tlHours));
  try {
    tlData = await api(`/api/schedules/timeline?hours=${tlHours}`);
    renderTimeline();
  } catch (e) {
    $("#timeline").innerHTML = `<div class="empty">Could not load timeline: ${esc(e.message)}</div>`;
  }
}

$("#tl-range").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-h]");
  if (!b) return;
  tlHours = Number(b.dataset.h);
  try { localStorage.setItem(TL_KEY, String(tlHours)); } catch { /* ignore */ }
  loadTimeline();
});

function fmtClock(d) { return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }
function fmtDayClock(d) { return d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }); }

function renderTimeline() {
  const el = $("#timeline");
  const tl = tlData;
  if (!tl) return;
  if (!tl.lanes.length) {
    el.innerHTML = `<div class="empty">Nothing planned – no model is running and no enabled schedule targets a model in this period.</div>`;
    return;
  }
  const t0 = new Date(tl.start).getTime();
  const t1 = new Date(tl.end).getTime();
  const width = Math.max(el.clientWidth, 280);
  const labelW = Math.min(200, Math.max(90, Math.round(width * 0.22)));
  const maxChars = Math.max(6, Math.floor((labelW - 14) / 7));
  const padR = 12, axisH = 34, laneH = 34, top = axisH + 4;
  const plotW = width - labelW - padR;
  const height = top + tl.lanes.length * laneH + 6;
  const x = (t) => labelW + ((t - t0) / (t1 - t0)) * plotW;

  // Ticks: smallest step leaving >= 64px between labels.
  const step = [1, 2, 3, 6, 12, 24].find((h) => (h * HOUR / (t1 - t0)) * plotW >= 64) ?? 24;
  const first = new Date(t0);
  first.setMinutes(0, 0, 0);
  const ticks = [];
  for (let t = first.getTime(); t <= t1; t += HOUR) {
    const d = new Date(t);
    if (t >= t0 && d.getHours() % step === 0) ticks.push(d);
  }

  const parts = [];
  // lane backgrounds + labels
  tl.lanes.forEach((lane, i) => {
    const y = top + i * laneH;
    if (i % 2 === 0) parts.push(`<rect class="tl-lane" x="${labelW}" y="${y}" width="${plotW}" height="${laneH}"/>`);
    const full = multiHost() ? `${maskAddr(lane.host)} · ${lane.name}` : lane.name;
    const name = full.length > maxChars ? full.slice(0, maxChars - 1) + "…" : full;
    parts.push(`<text class="tl-label" x="${labelW - 10}" y="${y + laneH / 2}" text-anchor="end" dominant-baseline="central"><title>${esc(full)}</title>${esc(name)}</text>`);
  });
  // grid + axis labels
  for (const d of ticks) {
    const tx = x(d.getTime());
    const midnight = d.getHours() === 0;
    parts.push(`<line class="tl-grid ${midnight ? "day" : ""}" x1="${tx}" x2="${tx}" y1="${axisH - 6}" y2="${height - 6}"/>`);
    const label = midnight || step >= 24 ? d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "numeric" }) : fmtClock(d);
    if (tx + label.length * 6.5 < width) parts.push(`<text class="tl-axis ${midnight ? "day" : ""}" x="${tx + 4}" y="${axisH - 12}">${esc(label)}</text>`);
  }
  // global events (stop all / free memory)
  for (const e of tl.events.filter((ev) => !ev.recipe_id)) {
    const tx = x(new Date(e.time).getTime());
    const where = multiHost() ? ` (${e.host_id ? maskAddr(hostName(e.host_id)) : "all hosts"})` : "";
    const tip = `${fmtDayClock(new Date(e.time))} · ${e.schedule}: ${ACTION_SHORT[e.action]}${where}`;
    // A host-scoped stop-all only crosses that host's rows.
    tl.lanes.forEach((lane, i) => {
      if (e.host_id && lane.host_id !== e.host_id) return;
      const y = top + i * laneH;
      parts.push(`<g class="tl-hit" data-sid="${e.schedule_id}" data-tip="${esc(tip)}">
        <line class="tl-global" x1="${tx}" x2="${tx}" y1="${y}" y2="${y + laneH}"/>
        <rect x="${tx - 5}" y="${y}" width="10" height="${laneH}" fill="transparent"/></g>`);
    });
  }
  // blocks
  tl.lanes.forEach((lane, i) => {
    const y = top + i * laneH + 6;
    const h = laneH - 12;
    const color = CARD_HEX[lane.color] ?? CARD_HEX[""];
    for (const b of lane.blocks) {
      const s = new Date(b.start), en = new Date(b.end);
      const bx = x(s.getTime()), bw = Math.max(2, x(en.getTime()) - bx - 2);
      const from = b.running_at_start ? "running now" : `${fmtDayClock(s)} (${b.started_by})`;
      const to = b.open_end ? "beyond this view" : `${fmtDayClock(en)} (${b.stopped_by})`;
      const hours = (en - s) / HOUR;
      const tip = `${lane.name}\n${from} → ${to}\n${hours >= 1 ? hours.toFixed(hours < 10 ? 1 : 0) + " h" : Math.round(hours * 60) + " min"}`;
      const text = bw > 96 ? `${b.running_at_start ? "now" : fmtClock(s)} – ${b.open_end ? "…" : fmtClock(en)}` : "";
      const sid = b.started_by_id ?? b.stopped_by_id ?? "";
      parts.push(`<g class="tl-hit" data-sid="${sid}" data-tip="${esc(tip)}">
        <rect class="tl-block ${lane.color === "black" ? "black" : ""}" x="${bx}" y="${y}" width="${bw}" height="${h}" rx="4" fill="${color}"/>
        ${text ? `<text class="tl-block-text" x="${bx + 8}" y="${y + h / 2}" dominant-baseline="central">${esc(text)}</text>` : ""}
        ${b.open_end ? `<path class="tl-open" d="M${bx + bw - 7} ${y + 4} l5 ${h / 2 - 4} l-5 ${h / 2 - 4}"/>` : ""}
      </g>`);
    }
  });
  // per-model event markers
  for (const e of tl.events.filter((ev) => ev.recipe_id)) {
    const i = tl.lanes.findIndex((l) => l.recipe_id === e.recipe_id);
    if (i < 0) continue;
    const tx = x(new Date(e.time).getTime());
    const y = top + i * laneH + 1;
    const tip = `${fmtDayClock(new Date(e.time))} · ${e.schedule}: ${ACTION_SHORT[e.action]} ${tl.lanes[i].name}`;
    parts.push(`<g class="tl-hit" data-sid="${e.schedule_id}" data-tip="${esc(tip)}">
      <path class="tl-mark" d="M${tx - 5} ${y} h10 l-5 6 z"/><rect x="${tx - 7}" y="${y - 2}" width="14" height="12" fill="transparent"/></g>`);
  }
  // now
  parts.push(`<text class="tl-now" x="${labelW + 2}" y="${height - 1}">now</text>`);

  el.innerHTML = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="Planned model runs over the next ${tlHours} hours">${parts.join("")}</svg>`;
}

const tip = $("#tl-tip");
$("#timeline").addEventListener("mousemove", (ev) => {
  const g = ev.target.closest("[data-tip]");
  if (!g) { tip.classList.add("hidden"); return; }
  tip.textContent = g.dataset.tip;
  tip.classList.remove("hidden");
  const pad = 14;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let left = ev.clientX + pad, topPos = ev.clientY + pad;
  if (left + w > window.innerWidth - 8) left = ev.clientX - w - pad;
  if (topPos + h > window.innerHeight - 8) topPos = ev.clientY - h - pad;
  tip.style.left = `${left}px`;
  tip.style.top = `${topPos}px`;
});
$("#timeline").addEventListener("mouseleave", () => tip.classList.add("hidden"));
$("#timeline").addEventListener("click", (ev) => {
  const g = ev.target.closest("[data-sid]");
  const s = g && state.schedules.find((x) => x.id === Number(g.dataset.sid));
  if (s) { tip.classList.add("hidden"); openScheduleEditor(s); }
});
let tlResize = null;
window.addEventListener("resize", () => { clearTimeout(tlResize); tlResize = setTimeout(renderTimeline, 150); });

/* ---------------- activity ---------------- */

async function loadEvents() {
  try {
    const events = await api("/api/events?limit=300");
    $("#events").innerHTML = events.length ? events.map((e) => `
      <tr>
        <td style="white-space:nowrap">${esc(fmtTime(e.ts))}</td>
        <td>${esc(e.source)}</td>
        <td>${esc(e.action)}</td>
        <td>${esc(blurText(e.target || ""))}</td>
        <td class="msg"><span class="${e.ok ? "ok" : "bad"}">${e.ok ? "✓" : "✗"}</span> ${esc(blurText(e.message))}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="empty">Nothing yet.</td></tr>`;
  } catch (e) { toast(e.message, "bad"); }
}
$("#btn-refresh-events").addEventListener("click", loadEvents);

/* ---------------- images tab ---------------- */

let imagesData = null;
const imagesView = { search: "", host: "", onlyNewer: false, onlyMissing: false, open: new Set() };

function imageAge(iso, short = false) {
  const d = new Date(iso);
  if (isNaN(d)) return short ? "?" : "age unknown";
  const days = Math.floor((Date.now() - d) / 86400e3);
  if (short) return days < 1 ? "today" : `${days}d`;
  return days < 1 ? "built today" : days === 1 ? "1 day old" : `${days} days old`;
}

async function loadImages() {
  const tbody = $("#images");
  if (!imagesData) tbody.innerHTML = `<tr><td colspan="3" class="empty">Asking the hosts…</td></tr>`;
  try {
    imagesData = await api("/api/images");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty">${esc(e.message)}</td></tr>`;
    return;
  }
  renderImages();
}

const imgKey = (img) => `${img.repository}:${img.tag}`;
const imgHasNewer = (img) => img.hosts.some((h) => h.other_tags.some((t) => t.newer));
const imgHasMissing = (img) => img.hosts.some((h) => h.state === "missing");

// Hosts that run at least one Docker recipe, in host order: the matrix columns.
function imageHosts() {
  const used = new Set(imagesData.images.flatMap((img) => img.hosts.map((h) => h.host_id)));
  return state.hosts.filter((h) => used.has(h.id));
}

function imageCellHtml(img, h) {
  if (!h) return `<span class="muted" title="No recipe on this host uses this image">–</span>`;
  if (h.state === "unknown") {
    return `<span class="muted" title="${esc(blurHostText(h.error || "not reachable", hostById(h.host_id)?.url))}">? unknown</span>`;
  }
  const others = h.other_tags.map((t) => `${t.tag} (${imageAge(t.created)}, ${t.size})`).join("\n");
  const base = h.state === "present"
    ? `<span class="ok" title="${esc(`${imageAge(h.local.created)} · ${h.local.size} · ${h.local.id}${others ? `\nOther tags here:\n${others}` : ""}`)}">✓ ${esc(imageAge(h.local.created, true))}</span>`
    : `<span class="bad" title="Not on this host yet – pulled on the first start${others ? `\nOther tags here:\n${esc(others)}` : ""}">✗ missing</span>`;
  const newer = h.other_tags.map((t, i) => [t, i]).filter(([t]) => t.newer);
  const chip = newer.length
    ? ` <button class="chip small newer-chip" data-img="${esc(imgKey(img))}" data-host="${h.host_id}" data-tag="${newer[0][1]}"
          title="${esc(`${newer[0][0].repository}:${newer[0][0].tag} · ${newer[0][0].size} · created ${fmtTime(newer[0][0].created)}`)} – click to switch this host's recipes to it">⬆ ${esc(newer[0][0].tag)} · ${esc(imageAge(newer[0][0].created, true))}</button>${newer.length > 1 ? ` <span class="muted" title="${esc(newer.slice(1).map(([t]) => t.tag).join(", "))}">+${newer.length - 1}</span>` : ""}`
    : "";
  return base + chip;
}

function renderImages() {
  if (!imagesData) return;
  const n = imagesData.native_recipes;
  $("#images-native").textContent = n ? `${n} native-process recipe${n === 1 ? "" : "s"} run without an image and ${n === 1 ? "is" : "are"} not listed.` : "";
  const hostSel = $("#images-host");
  const hosts = imageHosts();
  hostSel.innerHTML = `<option value="">All hosts</option>` + hosts.map((h) => `<option value="${h.id}">${esc(maskAddr(h.name))}</option>`).join("");
  hostSel.value = hosts.some((h) => String(h.id) === imagesView.host) ? imagesView.host : "";
  hostSel.closest("label").classList.toggle("hidden", hosts.length < 2);

  const all = imagesData.images;
  const nNewer = all.filter(imgHasNewer).length, nMissing = all.filter(imgHasMissing).length;
  $("#images-only-newer").textContent = `⬆ Newer available (${nNewer})`;
  $("#images-only-missing").textContent = `✗ Missing on a host (${nMissing})`;
  $("#images-only-newer").classList.toggle("on", imagesView.onlyNewer);
  $("#images-only-missing").classList.toggle("on", imagesView.onlyMissing);
  const nRecipes = new Set(all.flatMap((img) => img.recipes.map((r) => r.id))).size;
  const nRepos = new Set(all.map((img) => img.repository)).size;
  $("#images-summary").textContent = `${all.length} image${all.length === 1 ? "" : "s"} (${nRepos} name${nRepos === 1 ? "" : "s"}) · ${nRecipes} recipe${nRecipes === 1 ? "" : "s"}`;

  const cols = imagesView.host ? hosts.filter((h) => String(h.id) === imagesView.host) : hosts;
  const q = imagesView.search.trim().toLowerCase();
  const shown = all.filter((img) => {
    const hs = imagesView.host ? img.hosts.filter((h) => String(h.host_id) === imagesView.host) : img.hosts;
    if (imagesView.host && !hs.length) return false;
    if (imagesView.onlyNewer && !hs.some((h) => h.other_tags.some((t) => t.newer))) return false;
    if (imagesView.onlyMissing && !hs.some((h) => h.state === "missing")) return false;
    return !q || imgKey(img).toLowerCase().includes(q) || img.recipes.some((r) => r.name.toLowerCase().includes(q));
  });

  $("#images-head").innerHTML = `<tr><th>Image</th><th>Recipes</th>${cols.map((h) => `<th>${esc(maskAddr(h.name))}</th>`).join("")}</tr>`;
  const span = 2 + cols.length;
  const tbody = $("#images");
  if (!all.length) { tbody.innerHTML = `<tr><td colspan="${span}" class="empty">No Docker recipes yet.</td></tr>`; return; }
  if (!shown.length) { tbody.innerHTML = `<tr><td colspan="${span}" class="empty">No image matches the filters.</td></tr>`; return; }

  // Group the tags of one image name (e.g. three vLLM versions in use) under a header row.
  const byRepo = new Map();
  for (const img of shown) (byRepo.get(img.repository) ?? byRepo.set(img.repository, []).get(img.repository)).push(img);
  const rows = [];
  for (const [repo, imgs] of byRepo) {
    const tagsInUse = all.filter((i) => i.repository === repo).length;
    if (tagsInUse > 1) rows.push(`<tr class="img-repo"><td colspan="${span}"><span class="mono">${esc(repo)}</span> <span class="muted">· ${tagsInUse} tags in use</span></td></tr>`);
    for (const img of imgs) {
      const key = imgKey(img);
      const open = imagesView.open.has(key);
      const recipes = imagesView.host ? img.recipes.filter((r) => String(r.host_id) === imagesView.host) : img.recipes;
      rows.push(`<tr>
        <td class="mono img-ref">${tagsInUse > 1 ? "" : `${esc(repo)}<br>`}<b>:${esc(img.tag)}</b>
          <button class="btn small ghost img-pull" data-pull="${esc(key)}" title="docker pull this image on several hosts at once">⤓ Pull…</button></td>
        <td><button class="chip small" data-toggle="${esc(key)}" aria-expanded="${open}">${recipes.length} recipe${recipes.length === 1 ? "" : "s"} ${open ? "▾" : "▸"}</button></td>
        ${cols.map((h) => `<td class="img-cell">${imageCellHtml(img, img.hosts.find((x) => x.host_id === h.id))}</td>`).join("")}
      </tr>`);
      if (open) {
        const groups = new Map();
        for (const r of recipes) (groups.get(r.host_id) ?? groups.set(r.host_id, []).get(r.host_id)).push(r);
        const order = (hid) => state.hosts.findIndex((h) => h.id === hid);
        rows.push(`<tr class="img-recipes"><td></td><td colspan="${span - 1}">${[...groups].sort((a, b) => order(a[0]) - order(b[0])).map(([hid, rs]) =>
          `<div>${multiHost() ? `<b>${esc(maskAddr(hostName(hid)))}</b> ` : ""}${rs.map((r) => `<button class="chip small" data-recipe="${r.id}" title="Edit this recipe">${esc(r.name)}</button>`).join(" ")}</div>`).join("")}</td></tr>`);
      }
    }
  }
  tbody.innerHTML = rows.join("");
}

$("#btn-refresh-images").addEventListener("click", loadImages);
$("#images-search").addEventListener("input", (e) => { imagesView.search = e.target.value; renderImages(); });
$("#images-host").addEventListener("change", (e) => { imagesView.host = e.target.value; renderImages(); });
$("#images-only-newer").addEventListener("click", () => { imagesView.onlyNewer = !imagesView.onlyNewer; renderImages(); });
$("#images-only-missing").addEventListener("click", () => { imagesView.onlyMissing = !imagesView.onlyMissing; renderImages(); });

$("#images").addEventListener("click", async (ev) => {
  const pull = ev.target.closest("[data-pull]");
  if (pull) {
    const img = imagesData.images.find((i) => imgKey(i) === pull.dataset.pull);
    if (img) openTransfer({ kind: "pull", image: img.image }, img.hosts.filter((h) => h.state !== "present").map((h) => h.host_id));
    return;
  }
  const toggle = ev.target.closest("[data-toggle]");
  if (toggle) {
    const k = toggle.dataset.toggle;
    imagesView.open.has(k) ? imagesView.open.delete(k) : imagesView.open.add(k);
    renderImages();
    return;
  }
  const recipeBtn = ev.target.closest("[data-recipe]");
  if (recipeBtn) {
    const r = state.recipes.find((x) => x.id === Number(recipeBtn.dataset.recipe));
    if (r) openRecipeEditor(r);
    return;
  }
  const tagBtn = ev.target.closest(".newer-chip");
  if (!tagBtn) return;
  const img = imagesData.images.find((i) => imgKey(i) === tagBtn.dataset.img);
  const hostId = Number(tagBtn.dataset.host);
  const tag = img?.hosts.find((h) => h.host_id === hostId)?.other_tags[Number(tagBtn.dataset.tag)];
  if (!img || !tag) return;
  const newRef = `${tag.repository}:${tag.tag}`;
  const targets = state.recipes.filter((r) => r.host_id === hostId && img.recipes.some((x) => x.id === r.id));
  if (!targets.length) return;
  const names = targets.length > 8 ? `${targets.slice(0, 8).map((r) => `"${r.name}"`).join(", ")} and ${targets.length - 8} more` : targets.map((r) => `"${r.name}"`).join(", ");
  if (!confirm(`Switch ${targets.length} recipe${targets.length === 1 ? "" : "s"} on ${maskAddr(hostName(hostId))} (${names}) to\n${newRef}?\n\nA running model keeps the old image until you restart it.`)) return;
  try {
    for (const r of targets) await api(`/api/recipes/${r.id}`, { method: "PUT", body: recipeBodyFrom(r, { image: newRef }) });
    toast(targets.length === 1 ? `"${targets[0].name}" now uses ${newRef}` : `${targets.length} recipes now use ${newRef}`, "ok");
  } catch (e) {
    toast(e.message, "bad");
  }
  await loadRecipes();
  loadImages();
});

/* ---------------- models tab ---------------- */

let modelsData = null;
const modelsView = { search: "", host: "", onlyMissing: false, open: new Set() };

async function loadModels() {
  const tbody = $("#models");
  if (!modelsData) tbody.innerHTML = `<tr><td colspan="3" class="empty">Asking the hosts…</td></tr>`;
  try {
    modelsData = await api("/api/models");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty">${esc(e.message)}</td></tr>`;
    return;
  }
  renderModels();
}

function modelHosts() {
  const used = new Set(modelsData.models.flatMap((m) => m.hosts.map((h) => h.host_id)));
  return state.hosts.filter((h) => used.has(h.id));
}

const modelHasMissing = (m) => m.hosts.some((h) => h.state === "missing");

function modelCellHtml(m, h) {
  if (!h) return `<span class="muted" title="No recipe on this host uses this model">–</span>`;
  if (h.state === "unknown") {
    return `<span class="muted" title="${esc(blurHostText(h.error || "not reachable", hostById(h.host_id)?.url))}">? unknown</span>`;
  }
  if (h.state === "present") {
    return `<span class="ok" title="${esc(h.location || "present on this host")}">✓ present</span>`;
  }
  return `<span class="bad" title="${esc(h.location ? `Not found at: ${h.location}` : "Not on this host yet")}">✗ missing</span>`;
}

function renderModels() {
  if (!modelsData) return;
  const hostSel = $("#models-host");
  const hosts = modelHosts();
  hostSel.innerHTML = `<option value="">All hosts</option>` + hosts.map((h) => `<option value="${h.id}">${esc(maskAddr(h.name))}</option>`).join("");
  hostSel.value = hosts.some((h) => String(h.id) === modelsView.host) ? modelsView.host : "";
  hostSel.closest("label").classList.toggle("hidden", hosts.length < 2);

  const all = modelsData.models;
  const nMissing = all.filter(modelHasMissing).length;
  $("#models-only-missing").textContent = `✗ Missing on a host (${nMissing})`;
  $("#models-only-missing").classList.toggle("on", modelsView.onlyMissing);
  const nRecipes = new Set(all.flatMap((m) => m.recipes.map((r) => r.id))).size;
  $("#models-summary").textContent = `${all.length} model${all.length === 1 ? "" : "s"} · ${nRecipes} recipe${nRecipes === 1 ? "" : "s"}`;

  const cols = modelsView.host ? hosts.filter((h) => String(h.id) === modelsView.host) : hosts;
  const q = modelsView.search.trim().toLowerCase();
  const shown = all.filter((m) => {
    const hs = modelsView.host ? m.hosts.filter((h) => String(h.host_id) === modelsView.host) : m.hosts;
    if (modelsView.host && !hs.length) return false;
    if (modelsView.onlyMissing && !hs.some((h) => h.state === "missing")) return false;
    return !q || m.model.toLowerCase().includes(q) || m.recipes.some((r) => r.name.toLowerCase().includes(q));
  });

  $("#models-head").innerHTML = `<tr><th>Model</th><th>Recipes</th>${cols.map((h) => `<th>${esc(maskAddr(h.name))}</th>`).join("")}</tr>`;
  const span = 2 + cols.length;
  const tbody = $("#models");
  if (!all.length) { tbody.innerHTML = `<tr><td colspan="${span}" class="empty">No model recipes with a model set yet.</td></tr>`; return; }
  if (!shown.length) { tbody.innerHTML = `<tr><td colspan="${span}" class="empty">No model matches the filters.</td></tr>`; return; }

  const rows = shown.map((m) => {
    const open = modelsView.open.has(m.model);
    const recipes = modelsView.host ? m.recipes.filter((r) => String(r.host_id) === modelsView.host) : m.recipes;
    const out = [`<tr>
      <td class="mono">${esc(m.model)}</td>
      <td><button class="chip small" data-model-toggle="${esc(m.model)}" aria-expanded="${open}">${recipes.length} recipe${recipes.length === 1 ? "" : "s"} ${open ? "▾" : "▸"}</button>
        ${m.adoptable ? `<button class="btn small" data-adopt="${esc(m.model)}" title="Register this folder in the Weights registry and switch ${recipes.length === 1 ? "its recipe" : "these recipes"} to {weights}">→ Register</button>` : ""}</td>
      ${cols.map((h) => `<td class="img-cell">${modelCellHtml(m, m.hosts.find((x) => x.host_id === h.id))}</td>`).join("")}
    </tr>`];
    if (open) {
      const groups = new Map();
      for (const r of recipes) (groups.get(r.host_id) ?? groups.set(r.host_id, []).get(r.host_id)).push(r);
      const order = (hid) => state.hosts.findIndex((h) => h.id === hid);
      out.push(`<tr class="img-recipes"><td></td><td colspan="${span - 1}">${[...groups].sort((a, b) => order(a[0]) - order(b[0])).map(([hid, rs]) =>
        `<div>${multiHost() ? `<b>${esc(maskAddr(hostName(hid)))}</b> ` : ""}${rs.map((r) => `<button class="chip small" data-recipe="${r.id}" title="Edit this recipe">${esc(r.name)}</button>`).join(" ")}</div>`).join("")}</td></tr>`);
    }
    return out.join("");
  });
  tbody.innerHTML = rows.join("");
}

$("#btn-refresh-models").addEventListener("click", loadModels);
$("#models-search").addEventListener("input", (e) => { modelsView.search = e.target.value; renderModels(); });
$("#models-host").addEventListener("change", (e) => { modelsView.host = e.target.value; renderModels(); });
$("#models-only-missing").addEventListener("click", () => { modelsView.onlyMissing = !modelsView.onlyMissing; renderModels(); });

$("#models").addEventListener("click", async (ev) => {
  const adopt = ev.target.closest("[data-adopt]");
  if (adopt) {
    const m = modelsData.models.find((x) => x.model === adopt.dataset.adopt);
    const name = prompt(`Register “${m.model}” as weights named:\n(its recipes then mount {weights}; set the path for other hosts in the registry)`,
      m.model.replace(/\/+$/, "").split("/").pop());
    if (name === null) return;
    try {
      const w = await api("/api/weights/adopt", { method: "POST", body: { recipe_ids: m.recipes.map((r) => r.id), name } });
      toast(`Registered “${w.name}” – ${m.recipes.length} recipe${m.recipes.length === 1 ? "" : "s"} now use${m.recipes.length === 1 ? "s" : ""} {weights}`, "ok");
      await loadRecipes();
      await afterWeightsChange();
      loadModels();
    } catch (e) { toast(e.message, "bad"); }
    return;
  }
  const toggle = ev.target.closest("[data-model-toggle]");
  if (toggle) {
    const k = toggle.dataset.modelToggle;
    modelsView.open.has(k) ? modelsView.open.delete(k) : modelsView.open.add(k);
    renderModels();
    return;
  }
  const recipeBtn = ev.target.closest("[data-recipe]");
  if (recipeBtn) {
    const r = state.recipes.find((x) => x.id === Number(recipeBtn.dataset.recipe));
    if (r) openRecipeEditor(r);
  }
});

/* ---------------- keyboard shortcuts ---------------- */

const TAB_KEYS = { "1": "recipes", "2": "schedules", "3": "activity", "4": "hosts", "5": "images", "6": "models" };
const CARD_KEYS = { s: "start", x: "stop", r: "restart", l: "logs", e: "edit" };

document.addEventListener("keydown", (ev) => {
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const t = ev.target;
  if (t instanceof HTMLElement && (t.isContentEditable || /^(input|textarea|select|button)$/i.test(t.tagName))) return;
  if (document.querySelector("dialog[open]")) return;
  const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
  const card = document.activeElement instanceof Element ? document.activeElement.closest(".card") : null;
  if (card && k.startsWith("Arrow")) {  // move focus between cards
    const cards = $$("#recipes .card");
    const i = cards.indexOf(card);
    const next = (k === "ArrowRight" || k === "ArrowDown") ? cards[i + 1] : cards[i - 1];
    if (next) { next.focus(); ev.preventDefault(); }
    return;
  }
  if (card && CARD_KEYS[k]) {
    const btn = card.querySelector(`button[data-act="${CARD_KEYS[k]}"]:not([disabled])`);
    if (btn) { btn.click(); ev.preventDefault(); }
    return;
  }
  if (TAB_KEYS[k]) { $(`.tab[data-tab="${TAB_KEYS[k]}"]`).click(); ev.preventDefault(); return; }
  if (k === "/") { $('.tab[data-tab="recipes"]').click(); $("#recipe-search").focus(); ev.preventDefault(); return; }
  if (k === "n" && state.tab === "recipes") { openRecipeEditor(null); ev.preventDefault(); return; }
  if (k === "i" && state.tab === "recipes") { $("#btn-add-recipe").click(); ev.preventDefault(); return; }
  if (k === "q" && state.tab === "recipes") { openQuick(); ev.preventDefault(); return; }
  if (k === "t") { $("#btn-theme").click(); return; }
  if (k === "b") { $("#btn-blur").click(); return; }
  if (k === "?") { toast("Shortcuts: 1–6 tabs · / search · i add/import a recipe · n new recipe · q quick recipe · arrows move between cards · s start, x stop, r restart, l logs, e edit on the focused card · t theme · b blur"); return; }
});

/* ---------------- boot ---------------- */

async function boot() {
  state.meta = await api("/api/meta");
  $("#hub-tz").textContent = state.meta.timezone ? `: ${state.meta.timezone}` : "";
  $("#sel-runtime").innerHTML = Object.entries(state.meta.runtimes)
    .map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  renderFrameworkOptions("docker");
  $("#sel-action").innerHTML = Object.entries(state.meta.actions)
    .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  $("#swatches").innerHTML = state.meta.colors.map((c) =>
    `<button type="button" class="swatch" data-color="${c}" title="${c || "default"}" aria-label="${c || "default"} colour"></button>`).join("");
  await loadHosts();
  await loadWeightsList();
  await loadStartTimes();
  await loadRecipes();
  resumeJobs();
  setInterval(() => { if (!document.hidden) { loadRecipes(); loadHosts(); } }, 5000);
  // Keep the ⏳ elapsed counter ticking between the 5 s polls.
  setInterval(() => { if (!document.hidden && startWatch.size) renderRecipes(); }, 1000);
  setInterval(() => { if (!document.hidden && state.tab === "schedules") loadSchedules(); }, 15000);
}

boot().catch((e) => toast(`Failed to start: ${e.message}`, "bad"));
