// The release console's browser side. It renders the server's latest snapshot
// (Server-Sent Events from /api/stream) and turns clicks into POSTs. It keeps no
// state of its own beyond what is open, expanded, or being typed.
"use strict";

const ICONS = [
  "shield-check", "rocket", "server", "hand", "sparkles", "git-branch", "scroll-text", "plug", "lock",
  "check", "x", "circle-check", "circle-x", "loader-circle", "external-link", "chevron-down", "chevron-up",
  "refresh-cw", "power", "rotate-ccw", "triangle-alert", "clock", "circle-dashed", "ban", "arrow-right",
  "user-round", "layers", "braces",
];
const TONES = { platform: "violet", checkout: "sky", "release-eng": "orange", security: "pink" };
const params = new URLSearchParams(location.search);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

const S = {
  snap: null,
  icons: {},
  rendered: {},           // region -> last HTML, so unchanged regions aren't touched
  known: null,            // what was on screen before this snapshot (for entry animations)
  policyText: {},         // rule subject -> effect text, for the v1 to v2 wording change
  swaps: {},              // rule subject -> {from, until}
  envVersions: {},        // stage id -> deployed version, for the green flip
  workerStates: {},
  openSchema: null,
  auditOpen: false,
  expanded: new Set(),
  showRuleIds: false,
  lastEvent: null,
  lane: Promise.resolve(), // diagram animations, played in order
  toast: null,
};

// ------------------------------------------------------------------ helpers

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function icon(name, cls = "") {
  const svg = S.icons[name] || "";
  return svg.replace("<svg", `<svg aria-hidden="true" focusable="false" class="icon ${cls}"`);
}

async function loadIcons() {
  await Promise.all(ICONS.map(async (name) => {
    const text = await (await fetch(`icons/${name}.svg`)).text();
    S.icons[name] = text.replace(/<!--[\s\S]*?-->/, "").replace(/\sclass="[^"]*"/, "").trim();
  }));
  document.querySelectorAll("[data-icon]").forEach((el) => { el.outerHTML = icon(el.dataset.icon, el.className); });
}

// A version pill pops only when a version we already showed changes.
function bumped(before, now) {
  return before != null && now != null && before !== now && !reducedMotion.matches ? "bump" : "";
}

function setHTML(region, el, html) {
  if (S.rendered[region] === html) return false;
  S.rendered[region] = html;
  el.innerHTML = html;
  return true;
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function clock(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }) : "";
}

function seconds_until(iso) {
  return Math.max(0, Math.round((Date.parse(iso) - Date.now()) / 1000));
}

function countdown(iso) {
  return `<span data-until="${esc(iso)}"></span>`; // filled in by tick(), so the HTML stays stable
}

function tick() {
  document.querySelectorAll("[data-ago]").forEach((el) => { el.textContent = ago(el.dataset.ago); });
  document.querySelectorAll("[data-until]").forEach((el) => { el.textContent = `${seconds_until(el.dataset.until)}s`; });
}

function uiLink(href, label) {
  if (!href) return "";
  return `<a class="link-icon" href="${esc(href)}" target="_blank" rel="noopener" title="${esc(label)}" aria-label="${esc(label)}">${icon("external-link", "sm")}</a>`;
}

function staleChip(text) {
  return text ? `<span class="stale-chip" role="status">${icon("server", "sm")}${esc(text)}</span>` : "";
}

function staleNote(text) {
  return text ? `<div class="stale-note" role="status">${icon("server", "sm")}<span>${esc(text)}</span></div>` : "";
}

async function post(url, body = {}) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data = {};
  try { data = await response.json(); } catch { data = { error: response.statusText }; }
  return { ok: response.ok, status: response.status, data };
}

function showToast(message, detail = "", id = "toast-refusal") {
  clearTimeout(S.toast);
  const panel = $("#focus-panel");
  panel.querySelectorAll(".toast").forEach((t) => t.remove());
  const toast = document.createElement("div");
  toast.id = id;
  toast.setAttribute("role", "alert");
  toast.innerHTML = `${icon("ban")}<div>${esc(message)}${detail ? `<span class="detail">${esc(detail)}</span>` : ""}</div>`;
  placeToast(toast);
  S.toast = setTimeout(() => toast.remove(), 6000);
}

// A refusal sits in the card, just above the controls it answers; with no controls
// on screen, it floats at the top of the focus panel.
function placeToast(toast) {
  const slot = $("#focus-panel .toast-slot");
  toast.className = slot ? "toast in-card" : "toast";
  if (slot) slot.append(toast); else $("#focus-panel").prepend(toast);
}

async function act(url, body) {
  const { ok, status, data } = await post(url, body);
  if (!ok) showToast(data.error || `The console couldn't do that (HTTP ${status}).`, data.detail || "", status === 409 ? "toast-refusal" : "toast-error");
  return ok;
}

// ------------------------------------------------------------------ modes

function applyMode() {
  const mode = params.get("mode") || (S.snap && S.snap.mode_default) || "normal";
  document.body.classList.toggle("record", mode === "record");
  document.body.classList.toggle("guides", params.get("guides") === "1");
  fitCanvas();
}

function fitCanvas() {
  const canvas = $("#canvas");
  if (!document.body.classList.contains("record")) { canvas.style.transform = ""; return; }
  const scale = Math.min(1, innerWidth / 1920, innerHeight / 1080);
  canvas.style.transform = scale < 1 ? `scale(${scale})` : "";
}

function setMode(mode) {
  params.set("mode", mode);
  history.replaceState(null, "", `?${params}`);
  applyMode();
  S.rendered = {};
  render();
}

// ------------------------------------------------------------------ top bar

function renderTopbar(t) {
  setHTML("pills", $("#status-pills"),
    `<span class="pill neutral">${esc(t.profile)}</span>${t.reviewer ? `<span class="pill neutral">${esc(t.reviewer)}</span>` : ""}`);
  $("#service-name").textContent = t.service;

  const chips = t.workers.map((w) => {
    const cls = w.status === "up" ? "" : w.status;
    const state = w.status === "up" ? "up" : w.status === "down" ? "down" : "unknown";
    const control = w.status === "down"
      ? `<button class="btn small" data-worker="${w.name}" data-action="restart" ${t.supervisor ? "" : "disabled"} title="${t.supervisor ? "Restart this Worker" : "Kill and Restart need the demo supervisor (make run starts it)"}">${icon("rotate-ccw", "sm")}Restart</button>`
      : `<button class="btn small" data-worker="${w.name}" data-action="kill" ${t.supervisor ? "" : "disabled"} title="${t.supervisor ? "Kill this Worker (SIGKILL)" : "Kill and Restart need the demo supervisor (make run starts it)"}">${icon("power", "sm")}Kill</button>`;
    return `<div class="worker-chip tone-${w.tone} ${cls}" id="worker-${w.name}" title="${esc(w.detail || "")}">
      <span class="dot" aria-hidden="true"></span><span class="mono">${esc(w.name)}</span>
      <span class="state">${state}</span><span class="controls">${control}</span></div>`;
  }).join("");
  setHTML("workers", $("#workers"), chips);
  for (const w of t.workers) {
    if (S.workerStates[w.name] === "up" && w.status === "down" && !reducedMotion.matches) {
      const chip = $(`#worker-${w.name}`);
      chip.classList.add("flash");
    }
    S.workerStates[w.name] = w.status;
  }

  const s = t.scenario;
  // A control that can't run yet keeps readable text, shows a lock and a short reason,
  // and gives the full sentence on hover or focus.
  // The steps and their labels come from the server, which keeps them neutral until used.
  const STEP_ICONS = { join: "server", publish: "plug", mandate: "scroll-text" };
  const item = ({ id: key, label, done, done_label: doneText, blocked = null, blocked_short: why = "" }) => `<button role="menuitem" data-scenario="${key}" id="scenario-${key}"
      ${done ? "disabled" : ""} ${blocked ? `aria-disabled="true" aria-describedby="scenario-${key}-tip" class="blocked"` : ""}>
      ${icon(blocked ? "lock" : STEP_ICONS[key])}<span>${esc(label)}${blocked && why ? ` <span class="why">· ${esc(why)}</span>` : ""}</span>${done ? `<span class="done">${icon("check", "sm")}${esc(doneText)}</span>` : ""}
      ${blocked ? `<span class="tip" role="tooltip" id="scenario-${key}-tip">${esc(blocked)}</span>` : ""}</button>`;
  let menu = s.steps.map(item).join("");
  if (s.cloud_endpoint_link) {
    menu += `<a role="menuitem" href="${esc(s.cloud_endpoint_link)}" target="_blank" rel="noopener">${icon("external-link")}<span>Open Security Endpoint in Temporal Cloud</span></a>`;
  }
  menu += `<div class="note">Presenter controls. The audit records them as scenario controls.</div>`;
  setHTML("scenario", $("#scenario-menu"), menu);

  const me = t.signed_in || { name: "Nobody", title: "" };
  setHTML("signin-button", $("#signin-button"),
    `${icon("user-round")}<span class="who"><span>${esc(me.name)}</span><span class="title">${esc(me.title)}</span></span>${icon("chevron-down", "sm")}`);
  const record = document.body.classList.contains("record");
  const people = t.people.map((p) => `<button role="menuitemradio" aria-checked="${p.email === me.email}" data-signin="${esc(p.email)}">
      ${icon("user-round")}<span>${esc(p.name)} <span class="person-title">· ${esc(p.title)}</span></span>${p.email === me.email ? `<span class="check">${icon("check", "sm")}</span>` : ""}</button>`).join("");
  setHTML("signin", $("#signin-menu"), `${people}
    <button role="menuitemcheckbox" aria-checked="${record}" data-toggle-record>${icon("layers")}<span>Recording mode</span>${record ? `<span class="check">${icon("check", "sm")}</span>` : ""}</button>
    <div class="note">Demo sign-in: anyone here can act as anyone on the roster.</div>`);
}

// ------------------------------------------------------------------ registry

function renderRegistry(r) {
  const known = S.known && S.known.tools;
  const groups = r.groups.map((g) => {
    const rows = g.tools.length ? g.tools.map((tool) => {
      const isNew = known && !known.has(tool.name);
      const gate = tool.gate_icon
        ? `<span class="gate" style="--gate-tone: var(--${tool.gate_icon === "hand" ? "yellow" : "pink"})" title="${tool.gate_icon === "hand" ? "A person approves some calls" : "Another team reviews some calls"}">${icon(tool.gate_icon)}</span>`
        : "";
      return `<button class="tool-row tone-${g.tone} ${isNew ? "enter" : ""}" id="registry-row-${esc(tool.name)}" data-tool="${esc(tool.name)}" aria-haspopup="dialog">
        <span class="mono">${esc(tool.name)}</span><span class="pill tone-${g.tone}">${esc(tool.mode)}</span>${gate}</button>`;
    }).join("") : `<div class="tool-row ghost">${esc(g.team_name)}: no tools published</div>`;
    return `<div class="team-group tone-${g.tone}">
      <div class="team-name">${esc(g.team_name)}</div><div class="endpoint mono">${esc(g.endpoint)}</div>${rows}</div>`;
  }).join("");
  const missing = r.missing ? `<p class="secondary">The registry isn't set up yet. Run <span class="mono">make reset</span>.</p>` : "";
  const html = `<div class="card-head">${icon("plug", "lg")}<h2 id="registry-title">Tools on the gateway</h2><span class="spacer"></span>
      ${staleChip(r.stale)}${r.version != null ? `<span class="pill tone-violet ${bumped(S.known && S.known.registryVersion, r.version)}">v${r.version}</span>` : ""}</div>
    ${missing}${groups}`;
  setHTML("registry", $("#registry-panel"), html);
}

function openSchema(name) {
  const r = S.snap.registry;
  const group = r.groups.find((g) => g.tools.some((t) => t.name === name));
  const tool = group && group.tools.find((t) => t.name === name);
  if (!tool) return;
  const pop = $("#schema-popover");
  pop.className = `open tone-${group.tone}`;
  pop.innerHTML = `<h3>${icon("braces")}${esc(tool.name)}</h3>
    <button class="btn small close" data-close-schema aria-label="Close">${icon("x", "sm")}</button>
    <p>${esc(tool.description)}</p>
    <pre>${esc(JSON.stringify(tool.input_schema, null, 2))}</pre>
    <div class="caption">From ${esc(tool.contract)}. The contract is the tool definition.</div>`;
  const row = $(`#registry-row-${CSS.escape(name)}`).getBoundingClientRect();
  const canvas = $("#canvas").getBoundingClientRect();
  const scale = canvas.width / $("#canvas").offsetWidth || 1;
  pop.style.left = `${(row.right - canvas.left) / scale + 16}px`;
  pop.style.top = `${Math.max(88, (row.top - canvas.top) / scale - 40)}px`;
  S.openSchema = name;
  pop.querySelector("[data-close-schema]").focus();
}

function closeSchema() {
  const pop = $("#schema-popover");
  if (!S.openSchema) return;
  pop.className = "";
  const row = $(`#registry-row-${CSS.escape(S.openSchema)}`);
  S.openSchema = null;
  if (row) row.focus();
}

// ------------------------------------------------------------------ policy

function renderPolicy(p) {
  const now = Date.now();
  const lines = p.rules.map((rule) => {
    const before = S.policyText[rule.subject];
    // The wording animates only when the policy version we were showing changes.
    const versionChanged = S.known && S.known.policyVersion != null && S.known.policyVersion !== p.version;
    if (versionChanged && before && before.text !== rule.text && !reducedMotion.matches) {
      S.swaps[rule.subject] = { from: before, until: now + 1200 };
      setTimeout(render, 1250);
    }
    S.policyText[rule.subject] = { text: rule.text, tone: rule.tone, icon: rule.icon };
    const swap = S.swaps[rule.subject];
    const changing = swap && swap.until > now;
    const effect = changing
      ? `<span class="swap"><span class="was effect tone-${swap.from.tone}">${esc(swap.from.text)}</span><span class="now effect tone-${rule.tone}">${esc(rule.text)}</span></span>`
      : `<span class="effect">${esc(rule.text)}</span>`;
    return `<div class="rule tone-${rule.tone} ${changing ? "changed" : ""}" id="policy-rule-${esc(rule.id)}">
      <span class="icon-wrap">${icon(rule.icon)}</span>
      <div class="what"><div>${esc(rule.subject)}: ${effect}</div><div class="rule-id mono">${esc(rule.id)}</div></div></div>`;
  }).join("");
  const knownBlocked = S.known && S.known.blocked;
  const blocked = p.blocked.length ? `<div id="blocked-releases"><div class="eyebrow">${icon("lock", "sm")}Blocked releases</div>
      ${p.blocked.map((b) => `<div class="blocked-item ${knownBlocked && !knownBlocked.has(b.text) ? "enter" : ""}" title="${esc(b.reason)}">${icon("lock", "sm")}<span>${esc(b.text)}</span></div>`).join("")}</div>` : "";
  const html = `<div class="card-head">${icon("scroll-text", "lg")}<h2 id="policy-title">Policy</h2><span class="spacer"></span>
      ${staleChip(p.stale)}${p.version != null ? `<span class="pill tone-violet ${bumped(S.known && S.known.policyVersion, p.version)}">v${p.version}</span>` : ""}</div>
    <div class="${S.showRuleIds ? "show-rule-ids" : ""}">${lines}</div>${blocked}
    <button class="btn small rule-toggle" data-toggle-rule-ids aria-pressed="${S.showRuleIds}">${S.showRuleIds ? "Hide rule IDs" : "Rule IDs"}</button>`;
  setHTML("policy", $("#policy-panel"), html);
}

// ------------------------------------------------------------------ pipeline

function renderPipeline(p) {
  const knownStages = S.known && S.known.stages;
  const parts = [];
  p.stages.forEach((stage, i) => {
    const isNew = knownStages && !knownStages.has(stage.id);
    if (i > 0) parts.push(`<div class="arrow ${isNew ? "enter" : ""}" aria-hidden="true">${icon("arrow-right")}</div>`);
    parts.push(renderStage(stage, p, isNew));
  });
  const missing = p.missing ? `<p class="secondary">No release ledger yet. Run <span class="mono">make reset</span>.</p>` : "";
  const html = `<div class="card-head">${icon("git-branch", "lg")}<h2 id="pipeline-title">Release pipeline</h2>
      <span class="mono secondary">${esc(p.service)}</span><span class="spacer"></span>${staleChip(p.stale)}</div>
    ${missing}<div class="stages">${parts.join("")}</div>`;
  setHTML("pipeline", $("#pipeline"), html);
}

function renderStage(stage, p, isNew) {
  const id = `stage-${stage.id}`;
  if (stage.id === "candidates") {
    const items = p.candidates.map((c) => `<div class="candidate"><span class="ver">${esc(c.version)}${c.blocked ? icon("lock", "sm") : ""}</span><span class="sum">${esc(c.summary)}</span></div>`).join("");
    return `<div class="stage" id="${id}"><div class="stage-title">Candidates<span class="spacer"></span>${uiLink(stage.link, "Open the release ledger in Temporal UI")}</div>${items}</div>`;
  }
  if (stage.kind === "gate") {
    const bar = stage.progress != null && stage.decision == null ? `<div class="bar"><span style="width:${stage.progress}%; --tone: var(--${stage.tone})"></span></div>` : "";
    return `<div class="stage gate tone-${stage.tone} ${isNew ? "enter" : ""}" id="${id}">
      <div class="stage-title">${icon("shield-check")}${esc(stage.title)}<span class="spacer"></span>${uiLink(stage.link, "Open Security's review desk in Temporal UI")}</div>
      ${stage.version ? `<div class="value">${esc(stage.version)}</div>` : ""}
      ${stage.stale ? staleChip(stage.stale) : `<div class="gate-line">${esc(stage.line)}</div>${bar}`}</div>`;
  }
  const deployed = stage.deployed;
  const before = S.envVersions[stage.id];
  const flip = S.known && deployed && before && before !== deployed.version;
  if (deployed) S.envVersions[stage.id] = deployed.version;
  const inc = stage.incoming;
  let incoming = "";
  if (inc && inc.state === "deploying") {
    incoming = `<div class="incoming tone-orange"><span class="what">${icon("rocket", "sm")} Deploying ${esc(inc.version)}</span>
      <div class="bar"><span style="width:${inc.percent}%"></span></div><span class="step">${esc(inc.label)}</span></div>`;
  } else if (inc) {
    const cls = inc.state === "waiting_approval" ? "waiting" : "review";
    incoming = `<div class="incoming ${cls}"><span class="what">${icon(cls === "waiting" ? "hand" : "shield-check", "sm")} ${esc(inc.version)} · ${esc(inc.label)}</span></div>`;
  }
  const badge = stage.badge ? `<div class="badge tone-${stage.badge.tone}">${icon(stage.badge.icon, "sm")}<span>${esc(stage.badge.text)}</span></div>` : "";
  const link = inc && inc.link ? inc.link : stage.link;
  return `<div class="stage" id="${id}">
    <div class="stage-title">${esc(stage.title)}<span class="spacer"></span>${uiLink(link, inc && inc.link ? "Open the deploy in Temporal UI" : "Open the release ledger in Temporal UI")}</div>
    ${deployed ? `<div class="value ${flip && !reducedMotion.matches ? "flip" : ""}">${esc(deployed.version)}</div>
      ${inc ? "" : `<div class="by">deployed by ${esc(deployed.by)} · <span data-ago="${esc(deployed.at)}">${ago(deployed.at)}</span></div>`}` : `<div class="by">Nothing deployed</div>`}
    ${incoming}${badge}</div>`;
}

// ------------------------------------------------------------------ live calls

function renderCalls(c) {
  const region = $("#calls-diagram");
  const teamsKey = c.teams.map((t) => t.team).join(",");
  if (!region.querySelector("#diagram") || S.diagramTeams !== teamsKey) {
    S.diagramTeams = teamsKey;
    ["calls-stale", "node-agent", "node-gateway", "call-line", ...c.teams.map((t) => `node-${t.team}`)].forEach((k) => { S.rendered[k] = null; });
    region.innerHTML = `<div class="card-head">${icon("layers", "lg")}<h2 id="calls-title">Live calls</h2><span class="spacer"></span><span id="calls-stale"></span></div>
      <div class="diagram ${c.teams.length > 2 ? "compact" : ""}" id="diagram">
        <svg class="paths" id="diagram-paths"><g id="diagram-lines"></g><g id="diagram-dots"></g></svg>
        <div class="node tone-sky" id="node-agent"></div>
        <div class="node tone-violet" id="node-gateway"></div>
        ${c.teams.map((t, i) => `<div class="node team-node tone-${t.tone}" id="node-${t.team}" style="${teamPlace(i, c.teams.length)}"></div>`).join("")}
        <div id="diagram-labels"></div>
      </div>
      <div class="call-line" id="call-line"></div>`;
    S.rendered.paths = null;
    // Paths follow the nodes: redraw whenever the diagram or a node changes size.
    const observer = new ResizeObserver(() => S.snap && drawPaths(S.snap.calls));
    region.querySelectorAll("#diagram, .node").forEach((el) => observer.observe(el));
  }
  setHTML("calls-stale", $("#calls-stale"), staleChip(c.stale));
  setHTML("node-agent", $("#node-agent"), `<div class="node-title">${icon("sparkles")}${esc(c.agent.label)}</div><div class="sub">${esc(c.agent.client)}</div>`);
  const g = c.gateway;
  setHTML("node-gateway", $("#node-gateway"), `<div class="node-title">${icon("shield-check")}Gateway<span class="topbar-spacer"></span>${uiLink(g.link, "Open the session Workflow in Temporal UI")}</div>
    <div class="sub mono session">${esc(g.session_id || "no session yet")}</div>
    ${g.waiting_on_person ? `<span class="badge-wait" title="Waiting on a person">${icon("hand", "sm")}</span>` : ""}`);
  for (const t of c.teams) {
    const node = $(`#node-${t.team}`);
    const empty = !t.tools.length;
    node.classList.toggle("empty", empty);
    setHTML(`node-${t.team}`, node, `<div class="node-title">${icon(t.team === "security" ? "shield-check" : "rocket")}${esc(t.name)}</div>
      <div class="sub">Namespace <span class="mono">${esc(t.namespace)}</span></div>
      <div class="tools">${empty ? "No tools published" : t.tools.map((n) => `<span class="mono">${esc(n)}</span>`).join(" ")}</div>
      ${t.busy ? `<span class="badge-busy" title="Waiting on ${esc(t.name)}">${icon("loader-circle", "sm")}</span>` : ""}`);
  }
  setHTML("call-line", $("#call-line"), `${icon("arrow-right", "sm")}<span>${esc(c.line)}</span>`);
  drawPaths(c);
}

// The two demo teams sit at the top and bottom; any other number is spaced evenly.
function teamPlace(i, n) {
  if (n === 2) return "";
  return `top: calc(34px + ${i} * (100% - 34px) / ${n}); bottom: auto;`;
}

function centerOf(el, box) {
  const r = el.getBoundingClientRect();
  const scale = box.width / $("#diagram").offsetWidth || 1;
  return { x: (r.left - box.left) / scale, y: (r.top - box.top + r.height / 2) / scale, w: r.width / scale, h: r.height / scale };
}

function drawPaths(c) {
  const diagram = $("#diagram");
  if (!diagram) return;
  const box = diagram.getBoundingClientRect();
  const agent = centerOf($("#node-agent"), box);
  const gw = centerOf($("#node-gateway"), box);
  const gwRight = gw.x + gw.w;
  const teams = c.teams.map((t) => ({ t, at: centerOf($(`#node-${t.team}`), box) }));
  // The boundary sits just left of the team nodes, far enough for its Endpoint labels,
  // which are drawn level with each team node where its path crosses.
  const labelWidth = Math.max(...c.teams.map((t) => t.endpoint.length)) * 9.6 + 12;
  const teamLeft = Math.min(...teams.map((x) => x.at.x));
  const boundaryX = Math.max(gwRight + 24, teamLeft - 8 - labelWidth / 2);
  const height = diagram.offsetHeight;
  const shape = JSON.stringify([agent, gw, teams.map((x) => x.at), height]);
  if (S.rendered.paths === shape) return; // paths only move when the nodes do
  S.rendered.paths = shape;
  const width = diagram.offsetWidth;
  const lines = [`<rect class="team-side" x="${boundaryX}" y="0" width="${width - boundaryX}" height="${height}"></rect>`,
    `<line class="boundary" x1="${boundaryX}" y1="0" x2="${boundaryX}" y2="${height}"></line>`,
    `<path class="path" id="path-platform" d="M ${agent.x + agent.w} ${agent.y} L ${gw.x} ${gw.y}"></path>`];
  let labels = `<div class="boundary-caption secondary before" style="right:${width - boundaryX + 10}px">Namespace boundary</div>
    <div class="boundary-caption secondary after" style="left:${boundaryX + 10}px">Nexus Endpoints</div>`;
  const gwTop = gw.y - gw.h / 2;
  const gwBottom = gw.y + gw.h / 2;
  for (const { t, at } of teams) {
    // Each path crosses the boundary level with its team node, but never beside the
    // gateway node: its Endpoint label sits there and must not cover the gateway.
    const level = at.y < gw.y ? Math.min(at.y, gwTop - 16) : Math.max(at.y, gwBottom + 16);
    const y = Math.min(Math.max(level, at.y - at.h / 2 + 14), at.y + at.h / 2 - 14);
    const bend = (gwRight + boundaryX) / 2;
    lines.push(`<path class="path" id="path-${t.team}" d="M ${gwRight} ${gw.y} C ${bend} ${gw.y}, ${bend} ${y}, ${boundaryX} ${y} L ${at.x} ${y}"></path>`);
    // A label level with the gateway node moves right, clear of it.
    const half = (t.endpoint.length * 9.6) / 2 + 6;
    const lx = y > gwTop - 14 && y < gwBottom + 14 ? Math.max(boundaryX, gwRight + half + 6) : boundaryX;
    labels += `<div class="boundary-label mono" style="left:${lx}px; top:${y}px">${esc(t.endpoint)}</div>`;
  }
  $("#diagram-lines").innerHTML = lines.join("");
  $("#diagram-labels").innerHTML = labels;
}

function playEvents(events) {
  const newest = events.length ? events[events.length - 1].id : 0;
  if (S.lastEvent === null) { S.lastEvent = newest; return; } // never replay what happened before the page opened
  for (const e of events) {
    if (e.id <= S.lastEvent) continue;
    // One sequence: the call reaches the gateway, then goes out to the team, then returns.
    S.lane = S.lane.then(() => animate(e));
  }
  S.lastEvent = Math.max(S.lastEvent, newest);
}

function animate(e) {
  const pathId = e.kind === "submit" ? "path-platform" : `path-${e.team}`;
  const path = document.getElementById(pathId);
  if (!path) return Promise.resolve();
  const tone = e.kind === "submit" ? "violet" : TONES[e.team] || "violet";
  if (reducedMotion.matches) {
    path.classList.add("lit");
    setTimeout(() => path.classList.remove("lit"), 600);
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const layer = $("#diagram-dots");
    const length = path.getTotalLength();
    const back = e.kind === "back";
    const shape = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    shape.setAttribute("class", `${back ? "pulse" : "dot"} tone-${tone}`);
    shape.setAttribute("r", back ? "10" : "8");
    layer.appendChild(shape);
    path.classList.add("lit", `tone-${tone}`);
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / 600);
      const eased = 1 - Math.pow(1 - t, 2);
      const point = path.getPointAtLength((back ? 1 - eased : eased) * length);
      shape.setAttribute("cx", point.x);
      shape.setAttribute("cy", point.y);
      if (back) shape.setAttribute("r", String(8 + 6 * Math.sin(Math.PI * t)));
      if (t < 1) requestAnimationFrame(step);
      else {
        shape.remove();
        path.classList.remove("lit");
        resolve();
      }
    };
    requestAnimationFrame(step);
  });
}

// ------------------------------------------------------------------ audit

function renderAudit(a, cost) {
  setHTML("audit", $("#audit-bar"), `<div class="audit-row-bar">
      <button class="audit-toggle" data-toggle-audit aria-expanded="${S.auditOpen}" aria-controls="audit-drawer">${icon("scroll-text")}Audit · ${a.calls} ${a.calls === 1 ? "call" : "calls"}${icon(S.auditOpen ? "chevron-down" : "chevron-up", "sm")}</button>
      <span id="cost-meter">${icon("sparkles", "sm")}${esc(cost.text)}</span></div>`);
  const drawer = $("#audit-drawer");
  drawer.classList.toggle("open", S.auditOpen);
  if (!S.auditOpen) { S.rendered.drawer = null; drawer.innerHTML = ""; return; }
  const rows = a.rows.map((r, i) => {
    const key = `${r.kind}:${r.details.Call || r.tool}:${r.time}`;
    const open = S.expanded.has(key);
    const details = open ? `<tr class="details"><td colspan="4"><dl>${Object.entries(r.details).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}
        <dt>Temporal UI</dt><dd><a href="${esc(r.link)}" target="_blank" rel="noopener">Open ${icon("external-link", "sm")}</a></dd></dl></td></tr>` : "";
    return `<tr class="row" data-row="${esc(key)}" tabindex="0" aria-expanded="${open}">
        <td>${esc(clock(r.time))}</td>
        <td>${esc(r.who)}${r.via ? `<span class="via">${esc(r.via)}</span>` : ""}</td>
        <td><span class="tool mono">${esc(r.tool)}</span><span class="sum">${esc(r.summary)}</span></td>
        <td><span class="outcome tone-${r.tone}">${icon(r.icon, "sm")}${esc(r.outcome)}</span></td></tr>${details}`;
  }).join("");
  setHTML("drawer", drawer, `<div class="drawer-head">${icon("scroll-text", "lg")}<h2 id="audit-title">Audit</h2>
      <span class="secondary">Every call and scenario control, newest first</span><span class="topbar-spacer"></span>
      <button class="btn small" data-toggle-audit aria-label="Close the audit">${icon("x", "sm")}Close</button></div>
    <div class="table-scroll"><table id="audit-table"><thead><tr><th>Time</th><th>Who</th><th>Tool</th><th>Outcome</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="4">No calls yet.</td></tr>`}</tbody></table></div>`);
}

// ------------------------------------------------------------------ focus panel

function decisionRow(kind, target) {
  const me = S.snap.topbar.signed_in;
  return `<div class="decide-row"><label class="sr-only" for="reason">Reason</label>
      <input class="input" id="reason" placeholder="Reason" autocomplete="off">
      <button class="btn approve" data-decide="${kind}" data-target="${esc(target)}" data-decision="approved">${icon("check", "sm")}Approve</button>
      <button class="btn reject" data-decide="${kind}" data-target="${esc(target)}" data-decision="rejected">${icon("x", "sm")}Reject</button></div>
    <div class="deciding-as">Deciding as <b>${esc(me ? me.name : "nobody")}</b></div>`;
}

function stepRow(s) {
  const glyph = { done: "circle-check", running: "loader-circle", waiting_worker: "server", pending: "circle-dashed", failed: "circle-x" }[s.status] || "circle-dashed";
  let meta = "";
  if (s.status === "done") {
    // The mock reviewer spends no tokens, so its steps show durations only.
    const parts = [s.seconds != null ? `${s.seconds.toFixed(1)}s` : "", s.tokens != null ? `${s.tokens.toLocaleString()} tokens` : ""];
    meta = parts.filter(Boolean).join(" · ");
  } else if (s.status === "running") meta = s.attempt > 1 ? `attempt ${s.attempt}` : "running";
  else if (s.status === "waiting_worker") meta = `Waiting for a Worker · attempt ${s.attempt}`;
  else if (s.status === "failed") meta = "failed";
  else meta = "pending";
  const shimmer = s.status === "running" || s.status === "waiting_worker" ? "waiting-shimmer" : "";
  return `<li class="step ${s.status} ${shimmer}" id="step-${esc(s.step)}">${icon(glyph, "sm")}<span class="label">${esc(s.label)}</span><span class="meta">${esc(meta)}</span></li>`;
}

// Worker-down sentences live only here, in the focus panel; cards elsewhere show a short chip.
function staleNotes(f) {
  return (f.stale_notes || []).map(staleNote).join("");
}

function flightRow(item) {
  const bar = item.percent != null ? `<div class="bar"><span style="width:${item.percent}%"></span></div>` : "";
  return `<li class="flight tone-${esc(item.tone || "violet")}" id="flight-${esc(item.call_id)}"><div class="what">${esc(item.text)}</div><div class="status">${esc(item.status)}</div>${bar}</li>`;
}

function focusHTML(f) {
  switch (f.state) {
    case "approval":
      return { key: `approval:${f.call_id}`, html: `<div class="focus-card accent tone-yellow waiting-shimmer" id="focus-approval">
        <div class="eyebrow">${icon("hand", "sm")}Decision needed</div>${staleNotes(f)}
        <h2>${esc(f.title)}</h2>
        <div class="key">${esc(f.subject)}</div>
        <p class="lead">${esc(f.asked_by)}</p>
        <dl class="facts"><dt>Why it's held</dt><dd>${esc(f.why)}${S.showRuleIds ? `<span class="rule-ref mono">${esc(f.rule)}</span>` : ""}</dd>
          <dt>Who can decide</dt><dd>${esc(f.who)}</dd></dl>
        <div class="toast-slot"></div>
        ${decisionRow("approval", f.call_id)}</div>` };
    case "review":
    case "decision": {
      const findings = f.findings.length ? `<div class="findings"><div class="eyebrow">Findings</div>${f.findings.map((x) => `<div class="finding"><span class="chip ${esc(x.severity)}">${esc(x.severity)}</span><div><div class="title">${esc(x.title)}</div><div class="detail">${esc(x.detail)}</div></div></div>`).join("")}</div>` : "";
      const recTone = (f.recommendation || "").startsWith("Approve") ? "green" : "coral";
      const rec = f.recommendation ? `<p class="review-summary"><span class="recommendation tone-${recTone}">${icon("sparkles", "sm")}Recommendation: ${esc(f.recommendation)}.</span> ${esc(f.summary)}</p>` : "";
      // Once a decision is needed, the finished steps fold into one line; their detail was on screen during the review.
      const done = f.steps.filter((x) => x.status === "done");
      const seconds = done.reduce((sum, x) => sum + (x.seconds || 0), 0);
      const tokens = done.some((x) => x.tokens != null) ? done.reduce((sum, x) => sum + (x.tokens || 0), 0) : null;
      const steps = f.state === "decision" && done.length === f.steps.length
        ? `<div class="steps-done" id="review-steps">${icon("circle-check", "sm")}${done.length} of ${f.steps.length} steps done · ${seconds.toFixed(1)}s${tokens != null ? ` · ${tokens.toLocaleString()} tokens` : ""}</div>`
        : `<ol class="steps" id="review-steps">${f.steps.map(stepRow).join("")}</ol>`;
      const decision = f.state === "decision" ? `<div class="toast-slot"></div><div id="focus-decision"><div class="eyebrow">${icon("hand", "sm")}Decision needed</div>
          <div class="who">Who can decide: <b>${esc(f.who)}</b></div>${decisionRow("review", f.version)}</div>` : "";
      return { key: `review:${f.version}`, html: `<div class="focus-card accent tone-pink" id="focus-review">
        <div class="eyebrow">${icon("shield-check", "sm")}Security review<span class="topbar-spacer"></span>${uiLink(f.link, "Open Security's review desk in Temporal UI")}</div>
        <div class="key">${esc(f.subject)}</div>
        ${staleNotes(f)}
        <div class="review-body">${steps}${findings}${rec}</div>${decision}</div>` };
    }
    case "decided": {
      const approved = f.decision === "approved";
      return { key: `decided:${f.at}`, html: `<div class="focus-card accent tone-${approved ? "green" : "coral"}" id="focus-decided">
        <div class="eyebrow">${icon(approved ? "circle-check" : "circle-x", "sm")}${f.kind === "review" ? "Security decision" : "Decided"}</div>${staleNotes(f)}
        <div class="decided-key">${icon(approved ? "check" : "x", "lg")}${approved ? "Approved" : "Rejected"}</div>
        <div class="key">${esc(f.subject)}</div>
        <dl class="facts"><dt>Decided by</dt><dd><span class="decider">${esc(f.decider)}</span>${f.decider_title ? ` · ${esc(f.decider_title)}` : ""}</dd>
          <dt>When</dt><dd>${esc(clock(f.at))}</dd>${f.reason ? `<dt>Reason</dt><dd>${esc(f.reason)}</dd>` : ""}</dl></div>` };
    }
    case "retrying": {
      const next = f.next_attempt_at ? Math.max(0, Math.round((Date.parse(f.next_attempt_at) - Date.now()) / 1000)) : null;
      const line = f.circuit_breaker
        ? `Paused: circuit breaker open.${next != null ? ` Next attempt in ${countdown(f.next_attempt_at)}.` : ""}`
        : f.in_flight
          ? `Retrying the call to ${esc(f.team)}. Attempt ${f.attempt} in progress.`
          : `Retrying the call to ${esc(f.team)}. Attempt ${f.attempt}${next != null ? `, next try in ${countdown(f.next_attempt_at)}` : ""}.`;
      const explain = f.circuit_breaker ? `<p class="lead">Nexus stops calling a failing Endpoint for a while, then tries once.</p>` : `<p class="lead">Nexus keeps retrying with backoff until ${esc(f.team)}'s Endpoint answers.</p>`;
      return { key: `retrying:${f.endpoint}`, html: `<div class="focus-card accent tone-coral" id="focus-circuit-breaker">
        <div class="eyebrow">${icon("refresh-cw", "sm")}Retrying</div>${staleNotes(f)}<p class="retry-line">${line}</p>${explain}
        ${f.failure ? `<div><div class="secondary">Last failure</div><div class="error-string">${esc(f.failure)}</div></div>` : ""}</div>` };
    }
    case "blocked_endpoint": {
      const next = f.next_attempt_at ? Math.max(0, Math.round((Date.parse(f.next_attempt_at) - Date.now()) / 1000)) : null;
      return { key: `blocked:${f.endpoint}`, html: `<div class="focus-card accent tone-coral" id="focus-blocked-endpoint">
        <div class="eyebrow">${icon("lock", "sm")}Blocked by Endpoint policy</div>${staleNotes(f)}
        <h2>${esc(f.team)}'s Endpoint doesn't allow the gateway yet.</h2>
        <div class="error-string">${esc(f.failure)}</div>
        <p class="lead">${esc(f.team)} owns this allowlist.${f.retrying && next != null ? ` The gateway tries again in ${countdown(f.next_attempt_at)}.` : ""}</p>
        ${f.link ? `<a href="${esc(f.link)}" target="_blank" rel="noopener">Open ${esc(f.endpoint)} in Temporal Cloud ${icon("external-link", "sm")}</a>` : ""}</div>` };
    }
    default: {
      // "Now": every call in flight with its progress, then the latest outcome.
      const flights = (f.in_flight || []).length
        ? `<div class="eyebrow">In flight</div><ul class="flights">${f.in_flight.map(flightRow).join("")}</ul>` : "";
      const latest = f.last_outcome ? `<div class="eyebrow">Latest</div><p class="lead">${esc(f.last_outcome)}</p>` : "";
      return { key: "now", html: `<div class="focus-card" id="focus-idle">
        <div class="eyebrow">${icon("clock", "sm")}Now</div>${staleNotes(f)}
        <div class="idle-title">Nothing needs a decision.</div>
        ${flights}${latest}</div>` };
    }
  }
}

function renderFocus(f) {
  const panel = $("#focus-panel");
  const { key, html } = focusHTML(f);
  if (S.rendered.focus === html) return;
  const input = panel.querySelector("#reason");
  const keep = input && S.focusKey === key ? { value: input.value, focused: document.activeElement === input } : null;
  const changed = S.focusKey !== key;
  const toast = changed ? null : panel.querySelector(".toast");
  S.rendered.focus = html;
  S.focusKey = key;
  panel.innerHTML = html;
  if (changed && !reducedMotion.matches) panel.firstElementChild.classList.add("enter");
  if (toast) placeToast(toast);
  const fresh = panel.querySelector("#reason");
  if (keep && fresh) {
    fresh.value = keep.value;
    if (keep.focused) fresh.focus();
  }
}

// ------------------------------------------------------------------ problems

function renderProblems(problems) {
  setHTML("problems", $("#problems"), problems.map((p) => `<div class="problem" role="alert">${esc(p.text)}${p.detail ? `<span class="detail">${esc(p.detail)}</span>` : ""}</div>`).join(""));
}

// ------------------------------------------------------------------ render

function render() {
  const s = S.snap;
  if (!s) return;
  renderTopbar(s.topbar);
  renderRegistry(s.registry);
  renderPolicy(s.policy);
  renderPipeline(s.pipeline);
  renderCalls(s.calls);
  renderAudit(s.audit, s.cost);
  renderFocus(s.focus);
  renderProblems(s.problems || []);
  playEvents(s.calls.events || []);
  tick();
  if (s.registry.version == null) return; // nothing real on screen yet: no baseline for animations
  S.known = {
    tools: new Set(s.registry.groups.flatMap((g) => g.tools.map((t) => t.name))),
    stages: new Set(s.pipeline.stages.map((x) => x.id)),
    blocked: new Set(s.policy.blocked.map((b) => b.text)),
    registryVersion: s.registry.version,
    policyVersion: s.policy.version,
  };
}

function connect() {
  const source = new EventSource("api/stream");
  source.onmessage = (event) => {
    const first = !S.snap;
    S.snap = JSON.parse(event.data);
    if (first) applyMode();
    render();
    document.body.dataset.updated = String(Date.now());
  };
  source.onerror = () => {
    renderProblems([{ text: "The console lost its connection to its server. Reconnecting.", detail: "" }]);
    S.rendered.problems = null;
  };
}

// ------------------------------------------------------------------ input

function closeMenus(except) {
  for (const id of ["scenario", "signin"]) {
    if (id === except) continue;
    $(`#${id}-menu`).classList.remove("open");
    $(`#${id}-button`).setAttribute("aria-expanded", "false");
  }
}

function toggleMenu(id) {
  const menu = $(`#${id}-menu`);
  const open = !menu.classList.contains("open");
  closeMenus(id);
  menu.classList.toggle("open", open);
  $(`#${id}-button`).setAttribute("aria-expanded", String(open));
  if (open) { const first = menu.querySelector("button:not(:disabled), a"); if (first) first.focus(); }
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("button, [data-row], a");
  if (!el) { closeMenus(); if (!event.target.closest("#schema-popover")) closeSchema(); return; }
  if (el.id === "scenario-button") return toggleMenu("scenario");
  if (el.id === "signin-button") return toggleMenu("signin");
  if (el.dataset.scenario) {
    if (el.getAttribute("aria-disabled") === "true") return; // its tooltip says why
    closeMenus();
    const url = { join: "api/scenario/join", publish: "api/scenario/publish", mandate: "api/scenario/mandate" }[el.dataset.scenario];
    return act(url);
  }
  if (el.dataset.signin) { closeMenus(); return act("api/signin", { email: el.dataset.signin }); }
  if (el.hasAttribute("data-toggle-record")) { closeMenus(); return setMode(document.body.classList.contains("record") ? "normal" : "record"); }
  if (el.dataset.worker) return act(`api/workers/${el.dataset.worker}/${el.dataset.action}`);
  if (el.dataset.tool) { event.stopPropagation(); return S.openSchema === el.dataset.tool ? closeSchema() : openSchema(el.dataset.tool); }
  if (el.hasAttribute("data-close-schema")) return closeSchema();
  if (el.hasAttribute("data-toggle-rule-ids")) { S.showRuleIds = !S.showRuleIds; return render(); }
  if (el.hasAttribute("data-toggle-audit")) { S.auditOpen = !S.auditOpen; S.rendered.audit = null; return render(); }
  if (el.dataset.row) { toggleRow(el.dataset.row); return; }
  if (el.dataset.decide) {
    const reason = ($("#reason") || {}).value || "";
    const buttons = document.querySelectorAll("[data-decide]");
    buttons.forEach((b) => { b.disabled = true; });
    const url = el.dataset.decide === "approval"
      ? `api/approvals/${encodeURIComponent(el.dataset.target)}`
      : `api/reviews/${encodeURIComponent(el.dataset.target)}/decision`;
    const ok = await act(url, { decision: el.dataset.decision, reason });
    buttons.forEach((b) => { b.disabled = false; });
    if (ok && $("#reason")) $("#reason").value = "";
  }
});

function toggleRow(key) {
  if (S.expanded.has(key)) S.expanded.delete(key); else S.expanded.add(key);
  S.rendered.drawer = null;
  render();
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeMenus();
    closeSchema();
    if (S.auditOpen) { S.auditOpen = false; S.rendered.audit = null; render(); }
  }
  const row = event.target.closest && event.target.closest("[data-row]");
  if (row && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); toggleRow(row.dataset.row); }
});

addEventListener("resize", () => { fitCanvas(); if (S.snap) render(); });
setInterval(tick, 1000);

(async () => {
  applyMode();
  await loadIcons();
  await document.fonts.ready;
  connect();
})();
