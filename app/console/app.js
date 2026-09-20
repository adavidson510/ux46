/* UX46 console client.
   Renders real native history and real Vault rooms. Every piece of content
   from a runtime or a record reaches the page as text through textContent —
   never as markup. Agent replies additionally get a bounded Markdown pass
   (see "agent markdown" below) that builds elements itself; no HTML from a
   transcript is ever parsed or executed. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  csrf: "",
  mode: "",
  node: "",
  agent: "",           // the selected agent id; "" means this machine's own
  agents: [],          // the server's configured agent identities
  agentGen: 0,         // generation guard: a reply from the previous agent loses
  agentError: "",      // why the selected agent could not be reached, if so
  seq: 0,
  eventEpoch: "",
  eventRecovery: true,
  roomRead: 0,
  historyRead: 0,
  earlierRead: 0,
  freshness: {room: 0, history: 0, roomError: "", historyError: ""},
  room: null,          // room id
  rooms: new Map(),    // id -> room summary
  detail: null,        // room state payload
  items: [],           // ascending native items for the open room
  ids: new Set(),
  cursor: null,        // "load earlier" cursor
  complete: true,
  folded: true,
  openWork: {},
  openTurns: {},       // activity disclosure overrides for one turn
  openGroups: {},      // work runs the person opened, keyed by their first item id
  tail: [],            // newest native items, kept fresh even while reading back
  connKind: "",        // last connection state, so activity never implies progress offline
  connectionRefreshState: "", // successful Refresh in this live page; never auth proof
  accepted: null,      // {room, turn, at} between an accepted send and the next room read
  outcome: null,       // the last submission outcome worth showing on the activity line
  // shell: rails, the one open panel, and whether the chrome is put away.
  // `focus` is deliberately not a stored preference: it hides the drawers
  // without touching what they were, so leaving it restores them exactly.
  ui: {left: "wide", dock: "", overlay: null, focus: false},
  source: null,       // {room, roomLabel, itemId, text} shown in the source panel
  board: {key: "", version: 0, data: null, loading: false, saving: false,
          edits: new Map(), disclosures: new Map(), conflict: null, seq: 0, poll: 0},
  positions: {},
  following: true,
  newCount: 0,
  sel: null,           // {kind, index}
  lastTrigger: null,
  draft: {body: "", version: 0},
  draftDirty: false,
  sending: false,
  approvals: [],
  attention: null,
  device: "",
  listening: null,
  receipt: null,
  roomSeq: 0,          // generation guard: a stale room response never wins
  conflict: null,      // {room, mine, theirs} until the person chooses
  anchor: null,        // an item id we jumped to from search
  searchHits: [],
  openProjects: new Set(),
  workspace: null,      // the working-set projection, not the whole catalogue
  prefs: {},            // room id -> {pinned, hidden}, shared by every device
  projectViews: new Map(),  // project id -> which history view is expanded
  opened: [],           // rooms this device actually opened, newest first
  voice: {enabled: false, voices: [], reason: "", preferred: ""},
  audio: new Map(),
  historyUnavailable: null,
  roomError: null,
  roomGone: false,
  commandMode: "",
  commandFromDraft: false,
  roomRefreshing: false,
  catalogLoading: false,
  goalPanel: null,
  goalPanelStatus: null,
  goalPanelTarget: null,
  pending: [],          // queued messages, as the server reported them
  pendingSupport: "",   // "", "ok" or "missing" once the server has answered
  pendingEdit: null,    // {client_id, text} while one is being rewritten
  pendingDismissed: new Set(),   // settled ones this person put away
  attn: {order: "project", agents: new Map()},   // the attention register, per agent
  recoveries: [],       // unresolved sends, each pinned to the agent that made it
  attach: null,         // why taking this session failed, until it is acted on
  tabs: [],             // one strip of places across every configured agent
  // Saved desktops and session display names, kept for the workspace rather
  // than for this browser. `available` is null until the store has answered.
  desk: {version: 0, available: null, reason: "", aliases: new Map(), desktops: [], liveDesktops: [],
         raw: null, saving: false, conflict: null},
  // Which agent the left drawer is listing. Looking through one agent's
  // sessions is not the same as working in it, so this is deliberately not
  // `agent`: browsing never touches the open conversation, its draft, its
  // uploads or its tab.
  browse: {agent: ""},
  theme: "",            // "", "system", "dark" or "light"
};

/* A response only counts if it belongs to the room the person is still in. */
function stale(seq, roomId) { return seq !== state.roomSeq || state.room !== roomId; }

function viewFreshnessError() {
  const f = state.freshness;
  return f.roomError || f.historyError || (state.eventRecovery ? "Checking for missed updates" : "")
    || (!f.room || (state.detail?.controllable && !f.history) ? "Waiting for a current view" : "");
}

const FOLLOW_PX = 48;
const WORK_TYPES = new Set([
  "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
  "functionCallOutput", "reasoning", "webSearch", "plan", "unknown",
]);

/* Server errors that prove the submission was refused before any dispatch.
   Anything else — a dropped connection, an opaque failure — leaves the
   outcome unknown, and unknown is not "not sent". */
const PRE_DISPATCH_REFUSALS = new Set([
  "bad_client_id", "empty", "too_large", "not_controllable", "wrong_target",
  "held_by_room", "duplicate_mismatch", "not_owned", "held_elsewhere", "recovery_in_progress",
  "ownership_unavailable", "bad_room", "unknown_room", "bad_origin", "bad_csrf",
  "workspace_login_required", "denied", "bad_json", "bad_length", "short_body", "unsupported_encoding",
]);
const OUTBOX_KEY = "atlas.outbox";

/* ------------------------------------------------------------------ agents */
/* One app, several agents. The server publishes the list; the browser only
   ever names an id from it, and every API path is prefixed with that id so a
   remote agent answers with its own rooms, files, audio and history. The
   local agent keeps the unprefixed paths it has always used. */
let DEFAULT_AGENT = "local";
let localAgentResolved = false;
function agentId() { return state.agent || DEFAULT_AGENT; }
function isRemoteAgent() { return agentId() !== DEFAULT_AGENT; }
function apiUrl(path) {
  return isRemoteAgent() ? "/api/agents/" + agentId() + path : path;
}
/* Two agents can hold identically named orbit rooms. Every stored reference to
   a room — drafts, tabs, outbox, uploads — is therefore namespaced by agent so
   one agent's `orbit/build` can never be read as the other's. */
function agentKey(key) { return isRemoteAgent() ? key + "@" + agentId() : key; }
function agentLabel(id) {
  const found = (state.agents || []).find((a) => a.id === (id || agentId()));
  return (found && found.label) || id || agentId();
}

/* ------------------------------------------------------------ agent marks
   Every agent gets a face: a compact tile with a drawn mark and a stable
   colour, so a rail of them is a rail of recognisable places rather than a
   column of identical dots.

   The marks are geometry, not pictures. Each one is built here in the DOM out
   of a few paths — nothing is fetched, no font is relied on for a symbol, and
   there is no emoji lottery to lose in a different browser. An agent this
   console has never heard of still gets a face: its initials, and a colour
   picked from its id, so it is the same face on every reload and on every
   machine.

   Colour is never the whole signal. The name is on the button when the drawer
   is open, in the accessible label and the tooltip when it is a rail, and the
   mark itself differs in shape as well as hue. Availability is a separate
   badge in the corner rather than the identity itself: an agent that is off
   is still that agent. */

/* Four hands we know, and six tones so an unknown one still lands somewhere
   deliberate. Violet is the console's own colour and stays with the local
   agent; the rest are spaced around it. */
const AGENT_TONES = {local: 0, agent2: 1, agent3: 2, pane: 3, cp: 4, agent4: 1};
const AGENT_TONE_COUNT = 6;

function agentTone(id) {
  const known = AGENT_TONES[id];
  if (known !== undefined) return known;
  // Stable across reloads and machines: the id decides, nothing else.
  let hash = 0;
  for (const ch of String(id || "")) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash % AGENT_TONE_COUNT;
}

/* Two letters, from the name a person actually reads. */
function agentInitials(agent) {
  const source = String((agent && (agent.label || agent.id)) || "?");
  const words = source.replace(/([a-z])([A-Z])/g, "$1 $2")
    .split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const raw = words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2);
  return raw.toUpperCase();
}

/* The drawn marks, in a 20x20 box, as line work so they stay legible small and
   take their colour from the tile. Shape carries as much of the identity as
   colour does: a hull, a star, a bolt head, a window. */
const AGENT_GLYPHS = {
  agent4: [["path", "M10.7 3a7 7 0 1 0 6.3 10.3A6.4 6.4 0 0 1 10.7 3Z"],
           ["path", "M15.5 2.4v4.2M13.4 4.5h4.2"]],
  cp: [["path", "M6 3.5 3 10l3 6.5M14 3.5l3 6.5-3 6.5"], ["circle-solid", "10 10 2"]],
  local: [["path", "M10 2.6v4.2"], ["path", "M3.6 8.4 10 17.4l6.4-9"]],
  agent2: [["path", "M10 2.8v14.4"], ["path", "M3.8 6.4l12.4 7.2"],
          ["path", "M16.2 6.4L3.8 13.6"]],
  agent3: [["circle", "10 10 5.1"], ["circle-solid", "10 10 2"],
          ["path", "M10 1.7v2.3"], ["path", "M10 16v2.3"],
          ["path", "M1.7 10H4"], ["path", "M16 10h2.3"]],
  pane: [["rect", "3.4 3.4 13.2 13.2 2.6"], ["path", "M10 3.4v13.2"],
         ["path", "M3.4 10h13.2"]],
};

function agentGlyph(id) {
  const parts = AGENT_GLYPHS[id];
  if (!parts) return null;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "avatar-art");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  for (const [shape, spec] of parts) {
    const bits = spec.split(/\s+/);
    let node;
    if (shape === "path") {
      node = document.createElementNS(ns, "path");
      node.setAttribute("d", spec);
    } else if (shape === "rect") {
      node = document.createElementNS(ns, "rect");
      node.setAttribute("x", bits[0]); node.setAttribute("y", bits[1]);
      node.setAttribute("width", bits[2]); node.setAttribute("height", bits[3]);
      node.setAttribute("rx", bits[4]);
    } else {
      node = document.createElementNS(ns, "circle");
      node.setAttribute("cx", bits[0]); node.setAttribute("cy", bits[1]);
      node.setAttribute("r", bits[2]);
      if (shape === "circle-solid") node.setAttribute("class", "solid");
    }
    svg.appendChild(node);
  }
  return svg;
}

/* One agent's face. `size` is a class, not a measurement, so nothing here
   writes an inline style — this console's content policy forbids them, and a
   face that vanished under a policy would be a poor face.

   `status` is drawn as a corner badge when asked for, and is deliberately not
   the mark: the badge says whether the host answered, the mark says who it is,
   and the two are never the same claim. */
function agentAvatar(agent, opts) {
  const options = opts || {};
  const id = (agent && agent.id) || "";
  const tile = el("span", {
    class: "avatar t" + agentTone(id) + (options.size ? " " + options.size : ""),
    "aria-hidden": "true", data: {agent: id, mark: AGENT_GLYPHS[id] ? id : "initials"},
  });
  const glyph = agentGlyph(id);
  tile.appendChild(glyph || el("span", {class: "avatar-init", text: agentInitials(agent)}));
  if (options.status) {
    tile.appendChild(el("span", {class: "avatar-dot " + options.status}));
  }
  return tile;
}

/* What a person is told about an agent, in words, wherever the name itself
   may be hidden. Availability is spelled out rather than left to the colour
   of a four-pixel dot. */
function agentAbout(agent) {
  const availability = agent.availability || {};
  const off = agent.kind !== "local" && availability.state === "unavailable";
  const detail = [agent.runtime, agent.node].filter(Boolean).join(" · ");
  return {
    off,
    label: agent.label + (detail ? " · " + detail : "")
      + (off ? " · unavailable" : " · available"),
  };
}

/* ---------------------------------------------------------------- helpers */
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "on") for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
      else if (key === "data") for (const [k, v] of Object.entries(value)) node.dataset[k] = v;
      else node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function deviceId() {
  let id = "";
  try { id = window.localStorage.getItem("atlas.device") || ""; } catch (e) { id = ""; }
  if (!id) {
    id = (window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).slice(0, 12);
    try { window.localStorage.setItem("atlas.device", id); } catch (e) { /* private mode */ }
  }
  return id;
}

function clientId() {
  const raw = window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
  return raw.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40);
}

let csrfRefresh = null;
async function consoleFetch(url, options) {
  const response = await fetch(url, options);
  // Only this explicit pre-write rejection permits a retry. Network errors or
  // unknown outcomes never replay a message or upload automatically.
  if (response.status !== 403 || !options?.headers?.["X-Atlas-CSRF"]) return response;
  let refusal;
  try { refusal = await response.clone().json(); } catch (_) { return response; }
  if (refusal.error !== "bad_csrf") return response;
  if (!csrfRefresh) csrfRefresh = (async () => {
    const fresh = await fetch("/api/bootstrap", {headers: {Accept: "application/json"}});
    if (!fresh.ok) throw new Error("Could not refresh this page's connection");
    const bootstrap = await fresh.json();
    if (!bootstrap.csrf) throw new Error("Refresh this page to reconnect");
    state.csrf = bootstrap.csrf;
  })().finally(() => { csrfRefresh = null; });
  await csrfRefresh;
  return fetch(url, {...options, headers: {...options.headers, "X-Atlas-CSRF": state.csrf}});
}

// Normalize console-owned notices from adapters still using the old product name.
// Native transcript text never passes through this function.
function uiNotice(value) { return String(value || "").replace(/\bAtlas\b/g, "UX46"); }

async function api(path, options) {
  const opts = Object.assign({headers: {}}, options || {});
  opts.headers = Object.assign({"Accept": "application/json"}, opts.headers);
  if (opts.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Atlas-CSRF"] = state.csrf;
    opts.body = JSON.stringify(opts.body);
  }
  const response = await consoleFetch(opts.absolute ? path : apiUrl(path), opts);
  let payload = null;
  try { payload = await response.json(); } catch (e) { payload = null; }
  if (!response.ok) {
    const error = new Error(uiNotice((payload && payload.message) || response.statusText));
    error.code = (payload && payload.error) || String(response.status);
    error.status = response.status;
    error.detail = payload && payload.detail;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/* One of the static icons defined once in index.html. Nothing from a record
   ever names an icon: every call site below passes a literal id. */
function useIcon(id, className) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", className || "ic");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#" + id);
  svg.appendChild(use);
  return svg;
}

// Project marks are deterministic and shared by every session in a project.
// A room may deliberately override that project mark through the owner
// desktop state; otherwise a configured uploaded logo wins.
const projectMarks = new Map();
const MARK_AGENT_ID = /^[A-Za-z0-9._-]{1,64}$/;
const MARK_FILE_ID = /^[A-Za-z0-9_-]{1,128}$/;

function sessionMark(tab) {
  const owner = String(tab.agent || DEFAULT_AGENT);
  const room = String(tab.room || "");
  const marks = state.desk.raw && Array.isArray(state.desk.raw.sessionMarks)
    ? state.desk.raw.sessionMarks : [];
  const held = marks.find((entry) => entry && typeof entry === "object"
    && String(entry.agent || DEFAULT_AGENT) === owner && String(entry.room || "") === room);
  if (!held) return null;
  const kind = String(held.kind || "");
  const value = String(held.value || "").trim();
  const mark = el("span", {class: "project-mark session-mark", "aria-hidden": "true"});
  const fallback = () => {
    if (mark.isConnected) mark.replaceWith(projectMarkFallback(tab));
  };
  if (kind === "initials") {
    const chars = Array.from(value);
    if (!chars.length || chars.length > 4) return null;
    mark.textContent = value;
    return mark;
  }
  if (kind === "brand" && value === "ux46") {
    const image = el("img", {alt: "", src: "/brand/ux46-icon-1.svg"});
    image.addEventListener("error", fallback, {once: true});
    mark.appendChild(image);
    return mark;
  }
  if (kind === "file" && MARK_FILE_ID.test(value)) {
    const fileAgent = held.file_agent === undefined || held.file_agent === null || held.file_agent === ""
      ? owner : String(held.file_agent);
    if (!MARK_AGENT_ID.test(fileAgent)) return null;
    const image = el("img", {alt: "", src: agentPath(fileAgent, "/api/atlas/files/" + value + "/preview")});
    image.addEventListener("error", fallback, {once: true});
    mark.appendChild(image);
    return mark;
  }
  return null;
}

function projectMarkFallback(tab) {
  const project = String(tab.room || "").split("/")[0];
  const owner = tab.agent || DEFAULT_AGENT;
  const key = owner + "\0" + project;
  const words = String(tab.project || project).replace(/([a-z])([A-Z])/g, "$1 $2")
    .split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const initials = words.length > 1 ? words[0][0] + words[words.length - 1][0]
    : (words[0] || "?").slice(0, 2);
  let hash = 0;
  for (const ch of project) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  const mark = el("span", {class: "project-mark mark-" + hash % 6,
    "aria-hidden": "true", text: initials.toUpperCase()});
  if (!projectMarks.has(key)) {
    const promise = api(agentPath(owner, "/api/projects/" + encodeURIComponent(project) + "/pref"), {absolute: true})
      .then(data => data.pref && data.pref.icon_file_id).catch(() => null);
    projectMarks.set(key, promise);
  }
  projectMarks.get(key).then(id => {
    if (!id || !/^[A-Za-z0-9_-]{1,128}$/.test(id)) return;
    const image = el("img", {alt: "", src: agentPath(owner, "/api/atlas/files/" + id + "/preview")});
    image.addEventListener("error", () => { mark.textContent = initials.toUpperCase(); }, {once: true});
    mark.replaceChildren(image);
  });
  return mark;
}

function projectMark(tab) {
  // This must happen before the project preference request: an exact room
  // mark cannot be replaced by a late project-logo response.
  return sessionMark(tab) || projectMarkFallback(tab);
}

function shortId(value) { return value ? String(value).slice(0, 8) : ""; }
function when(seconds) {
  if (!seconds) return "";
  const d = new Date(seconds * 1000);
  return d.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
}

function setConn(text, kind) {
  const node = $("#connState");
  node.textContent = text;
  node.className = "chip-state" + (kind ? " " + kind : "");
  state.connKind = kind || "";
  renderActivity();
}

/* The composer carries no permanent status footer. This line exists only
   while there is a real delivery or draft state to report; the routine
   "nothing to say" case leaves the field alone. The full picture, including
   the saved draft version, is always in Session options → Connection details. */
function setSendState(text, kind) {
  const node = $("#sendState");
  node.textContent = uiNotice(text);
  node.className = "cstate " + (kind || "ok");
  node.hidden = !text;
}

/* A delivery outcome is the most important thing in the footer: hold it until
   the person types again, so a routine state refresh cannot erase it. */
function setReceipt(text, kind) {
  state.receipt = {text, kind, until: Date.now() + 20000};
  setSendState(text, kind);
}
function clearReceipt() { state.receipt = null; }

/* ----------------------------------------------------------- session tabs
   One strip across every configured agent. A tab is a place — an agent and a
   room — not a row in whichever catalogue happens to be loaded, so choosing a
   different agent to look through never empties it.

   Each tab therefore carries the little it needs to draw itself: the agent's
   label, the project, the session title, and whether that agent said the room
   was controllable. Two records of one native conversation are one tab, but
   only within an agent: two agents can hold identically named rooms and mean
   entirely different work, so identity is always scoped by agent. */

const TABS_KEY = "atlas.tabs";        // the old per-agent lists, left in place
const TABS_KEY_V2 = "atlas.tabs.v2";  // the unified list this reads and writes
const ROOM_ID_SHAPE = /^[^/]+\/[^/]+$/;

function tabKey(agent, room) { return (agent || DEFAULT_AGENT) + "\0" + room; }
function tabIdentity(tab) {
  return (tab.agent || DEFAULT_AGENT) + "\0" + (tab.identity || tab.room);
}
function isActiveTab(tab) { return tab.agent === agentId() && tab.room === state.room; }
function findTab(agent, room) {
  return state.tabs.find((tab) => tab.agent === agent && tab.room === room) || null;
}

/* A stored tab, believed only as far as its shape. */
function cleanTab(raw, configured) {
  if (!raw || typeof raw !== "object") return null;
  const agent = String(raw.agent || DEFAULT_AGENT);
  const room = String(raw.room || "");
  if (!ROOM_ID_SHAPE.test(room)) return null;
  const tab = {
    agent, room,
    project: String(raw.project || ""),
    title: String(raw.title || ""),
    cap: String(raw.cap || ""),
    identity: String(raw.identity || ""),
    controllable: raw.controllable === undefined ? null : Boolean(raw.controllable),
    // A name the person gave this place. It is a label, never a session rename.
    customLabel: raw.customLabel ? String(raw.customLabel) : "",
    // An agent this console does not serve is still shown, saying so, rather
    // than disappearing without explanation.
    unavailable: configured.has(agent) ? "" : "this console does not serve " + agent,
  };
  if (!tab.customLabel) delete tab.customLabel;
  return tab;
}

function configuredIds() {
  const ids = new Set((state.agents || []).map((a) => a.id));
  ids.add(DEFAULT_AGENT);
  return ids;
}

/* Read the unified list, or build it once from whatever the per-agent lists
   already held. The old keys are never written to and never removed: this
   migration has to be safe to run against a browser that may be rolled back. */
function readTabs() {
  const configured = configuredIds();
  let stored = null;
  try { stored = JSON.parse(window.localStorage.getItem(TABS_KEY_V2) || "null"); }
  catch (e) { stored = null; }
  if (stored && Array.isArray(stored.tabs)) {
    const seen = new Set();
    const tabs = [];
    for (const raw of stored.tabs) {
      const tab = cleanTab(raw, configured);
      if (!tab || seen.has(tabKey(tab.agent, tab.room))) continue;
      seen.add(tabKey(tab.agent, tab.room));
      tabs.push(tab);
    }
    return tabs;
  }
  const migrated = [];
  const seen = new Set();
  for (const agent of [...configured]) {
    const key = agent === DEFAULT_AGENT ? TABS_KEY : TABS_KEY + "@" + agent;
    let rooms = null;
    try { rooms = JSON.parse(window.localStorage.getItem(key) || "null"); }
    catch (e) { rooms = null; }
    if (!Array.isArray(rooms)) continue;
    for (const room of rooms) {
      if (typeof room !== "string" || !ROOM_ID_SHAPE.test(room)) continue;
      if (seen.has(tabKey(agent, room))) continue;
      seen.add(tabKey(agent, room));
      migrated.push({agent, room, project: "", title: "", cap: "", identity: "",
                     controllable: null, unavailable: ""});
    }
  }
  return migrated;
}

function writeTabs() {
  try {
    window.localStorage.setItem(TABS_KEY_V2,
      JSON.stringify({version: 2, tabs: state.tabs}));
  } catch (e) { /* private mode: the strip is simply per-session */ }
  // The strip is the same on every device unless this one opted out, so the
  // one place it is written locally is the one place the change is published.
  publishTabs();
}

/* Everything a tab knows about itself, from whichever payload described the
   room. Metadata is only ever added: a room read for one agent must not blank
   what another agent's tab already showed. */
function describeTab(tab, room) {
  if (!room) return tab;
  if (room.project_name) tab.project = room.project_name;
  if (room.title || room.session) tab.title = room.title || room.session;
  const cap = room.capability_short || room.capability_label;
  if (cap) tab.cap = cap;
  if (room.identity_key) tab.identity = room.identity_key;
  if (room.controllable !== undefined) tab.controllable = Boolean(room.controllable);
  return tab;
}

/* Put a place on the strip, or refresh the one already there. New tabs join
   the end; nothing reorders, because a strip that rearranges itself when you
   look at it is a strip you cannot aim at. */
function openTab(agent, room, meta) {
  if (!room || !ROOM_ID_SHAPE.test(room)) return null;
  const id = agent || DEFAULT_AGENT;
  if (!configuredIds().has(id)) return null;
  let tab = findTab(id, room);
  if (!tab) {
    // One native conversation is one tab, within its own agent.
    const identity = meta && meta.identity_key;
    if (identity) {
      const same = state.tabs.find(
        (other) => other.agent === id && other.identity === identity);
      if (same) tab = same;
    }
  }
  if (tab) {
    if (tab.room !== room) tab.room = room;
    describeTab(tab, meta);
    writeTabs();
    return tab;
  }
  tab = describeTab({agent: id, room, project: "", title: "", cap: "", identity: "",
                     controllable: null, unavailable: ""}, meta);
  state.tabs.push(tab);
  // Open work stays visible until the person closes it. The strip scrolls;
  // opening another conversation must never silently hide an older one.
  writeTabs();
  return tab;
}

function forgetTab(agent, room) {
  const before = state.tabs.length;
  state.tabs = state.tabs.filter((tab) => !(tab.agent === agent && tab.room === room));
  if (state.tabs.length !== before) writeTabs();
}

/* --------------------------------------------------------------- rendering */
/* The dot says only what some agent actually reported. A live request from
   the attention register counts for any agent; the open conversation can also
   speak for itself. Nothing here infers activity from reachability. */
function tabDot(tab) {
  const entry = state.attn.agents.get(tab.agent);
  if (entry && entry.state === "ready") {
    const row = entry.rows.find((r) => r.room === tab.room);
    if (row && row.rank >= 4) return "needs";
  }
  if (isActiveTab(tab)) {
    if ((state.approvals || []).some((a) => a.room === tab.room)) return "needs";
    if (state.detail && state.detail.controllable) return "live";
    return "";
  }
  return tab.controllable ? "live" : "";
}

function tabNode(tab, compact) {
  const active = isActiveTab(tab);
  const name = tabLabel(tab);
  const canonical = canonicalLabel(tab);
  const label = agentLabel(tab.agent);
  const many = new Set(state.tabs.map((other) => other.agent)).size > 1;
  const where = [label, tab.project, canonical, tab.cap].filter(Boolean).join(" · ")
    + (name !== canonical ? " — shown as " + name : "");
  const wrap = el("span", {
    class: "tabwrap" + (active ? " on" : "") + (tab.closing ? " closing" : "")
      + (tab.unavailable ? " away" : ""),
    data: {agent: tab.agent, room: tab.room},
  });
  wrap.appendChild(el("button", {
    class: "tab", type: "button", role: "tab", disabled: Boolean(tab.unavailable),
    "aria-selected": String(active),
    title: tab.unavailable ? where + " — " + tab.unavailable
      : where + (tab.closeError ? " — " + tab.closeError : ""),
    data: {agent: tab.agent, room: tab.room},
    on: {
      click: () => {
        // A tab that was just dragged has not been clicked.
        if (document.body.classList.contains("tab-dragging")) return;
        void openTabForTyping(tab.agent, tab.room);
      },
      pointerdown: (event) => { if (!compact) tabPointerDown(event, tab, wrap); },
      contextmenu: (event) => {
        if (compact) return;
        event.preventDefault();
        openTabMenu(tab, wrap);
      },
      keydown: (event) => {
        if (compact) return;
        if (event.key === "F2") { event.preventDefault(); openTabMenu(tab, wrap); return; }
        if (event.shiftKey && event.key === "ArrowLeft") {
          event.preventDefault(); moveTab(tab, -1); return;
        }
        if (event.shiftKey && event.key === "ArrowRight") {
          event.preventDefault(); moveTab(tab, 1);
        }
      },
    },
  }, [
    el("span", {class: "dot " + tabDot(tab), "aria-hidden": "true"}),
    projectMark(tab),
    el("span", {class: "tname", text: name}),
    // Whose work this is, said quietly and only when it could be anyone's.
    many && !compact ? el("span", {class: "towner", text: label}) : null,
  ]));
  // Real buttons beside the tab rather than inside it: one control, one job.
  if (!compact) {
    wrap.appendChild(el("button", {
      class: "tmenu", type: "button",
      title: "Rename or move " + name,
      "aria-label": "Options for " + name,
      on: {click: (event) => { event.stopPropagation(); openTabMenu(tab, wrap); }},
    }, [el("span", {"aria-hidden": "true", text: "\u22ef"})]));
    wrap.appendChild(el("button", {
      class: "tclose closebtn", type: "button", disabled: tab.closing,
      title: tab.controllable === false || tab.unavailable
        ? "Close this tab" : "Close this tab and release " + canonical + " on " + label,
      "aria-label": "Close " + name + " on " + label,
      on: {click: () => closeTab(tab)},
    }, [useIcon("i-close")]));
  }
  return wrap;
}

function renderMobileTabs(tabs) {
  const select = $("#mobileTabSelect");
  const active = tabs.find(isActiveTab);
  $("#mobileTabName").textContent = active ? tabLabel(active) : "Choose conversation";
  const options = tabs.map((tab) => ({
    value: JSON.stringify([tab.agent || DEFAULT_AGENT, tab.room]),
    label: tabLabel(tab) + " · " + agentLabel(tab.agent)
      + (tab.unavailable ? " · unavailable" : ""),
    disabled: Boolean(tab.unavailable || tab.closing),
  }));
  const signature = JSON.stringify(options);
  // Polling must not rebuild an unchanged picker while it is open on a phone.
  if (select.dataset.options !== signature) {
    select.replaceChildren(el("option", {value: "", text: "Choose conversation", disabled: true}),
      ...options.map((option) => el("option", {
        value: option.value, text: option.label, disabled: option.disabled,
      })));
    select.dataset.options = signature;
  }
  select.value = active ? JSON.stringify([active.agent || DEFAULT_AGENT, active.room]) : "";
  select.disabled = !tabs.length;
  select.onchange = async () => {
    if (!select.value) return;
    const [agent, room] = JSON.parse(select.value);
    await openTabForTyping(agent, room);
    renderTabs();
  };
}

function renderTabs() {
  $("#tabs").classList.toggle("crowded", state.tabs.length > 6);
  // Rebuilding the strip under a finger would drop the tab being moved.
  if (tabDrag && tabDrag.active) return;
  const drawn = [];
  const identities = new Set();
  for (const tab of state.tabs) {
    const key = tabIdentity(tab);
    if (identities.has(key)) continue;
    identities.add(key);
    drawn.push(tab);
  }
  renderMobileTabs(drawn);
  for (const [host, compact] of [[$("#tabs"), false], [$("#focusRooms"), true]]) {
    host.replaceChildren();
    for (const tab of drawn) host.appendChild(tabNode(tab, compact));
  }
  const selected = $("#tabs").querySelector('[aria-selected="true"]');
  if (selected && selected.scrollIntoView) {
    selected.scrollIntoView({block: "nearest", inline: "nearest"});
  }
  // The register is drawn around this desktop's open tabs, so a change to the
  // strip is a change to the register. It redraws only if the view differs.
  if (state.ui.dock === "attention") renderAttentionPanel();
}

/* ---------------------------------------------------------- opening a place
   One way in for a tab, a room row, or an attention card: name the agent and
   the room, and end up in exactly that conversation with a tab for it. */
let tabTypingIntent = 0;
async function openTabForTyping(agent, room) {
  const intent = ++tabTypingIntent;
  const target = agent || DEFAULT_AGENT;
  const opened = await openSession(target, room, {toTail: true, connect: true});
  const draft = $("#draft");
  if (opened && intent === tabTypingIntent && agentId() === target && state.room === room && !draft.disabled) {
    draft.focus({preventScroll: true});
  }
  return opened;
}

async function openSession(agent, room, opts) {
  const id = agent || DEFAULT_AGENT;
  if (!room) return false;
  // What the caller asked for travels the whole way. An agent switch used to
  // drop it, so opening somebody else's session connected differently from
  // opening one of this agent's — which is exactly the kind of difference
  // nobody can see and everybody trips over.
  const wanted = opts || {toTail: true};
  if (id !== agentId()) {
    const ok = await switchAgent(id, room, wanted);
    if (!ok) return false;
    return state.room === room;
  }
  return selectRoom(room, wanted);
}

/* ------------------------------------------------------- saved desktops
   Two different things live here, and keeping them apart is the whole design.

   The strip's *current* order is this browser's, saved locally with the tabs
   themselves. It changes constantly and belongs to the device.

   A *desktop* is an arrangement someone chose to keep: which conversations, in
   which order, which one was open, and the few preferences that go with them.
   Those, and the display names given to sessions, live on the central host
   under one owner, so they are the same on every device.

   Nothing in here attaches, releases, sends or prompts. Renaming a tab renames
   a label. Loading a desktop rearranges a strip. The conversations underneath
   carry on exactly as they were, and the hint after a load says so. */

const DESKTOP_PATH = "/api/desktop-state";
const DESKTOP_SCHEMA = 1;

function aliasKey(agent, room) { return (agent || DEFAULT_AGENT) + "\0" + room; }

/* The envelope, as the server hands it over. Unknown fields are kept exactly
   as they arrived so a later version of this store loses nothing by passing
   through an older console. */
function adoptDesktopState(envelope) {
  const state_ = (envelope && envelope.state) || {};
  state.desk.version = Number(envelope && envelope.version) || 0;
  state.desk.raw = state_;
  state.desk.available = true;
  state.desk.reason = "";
  state.desk.aliases = new Map();
  for (const entry of Array.isArray(state_.aliases) ? state_.aliases : []) {
    if (!entry || !entry.room) continue;
    const label = String(entry.label || "").trim();
    if (!label) continue;
    state.desk.aliases.set(aliasKey(entry.agent, entry.room), label);
  }
  state.desk.desktops = Array.isArray(state_.desktops) ? state_.desktops : [];
  state.desk.liveDesktops = liveDesktops(state_);
  renderChapterNotice();
  renderNotes();
}

async function loadDesktopState() {
  try {
    adoptDesktopState(await api(DESKTOP_PATH, {absolute: true}));
  } catch (error) {
    // Before the store exists this console still works; it simply cannot claim
    // that anything was kept for the workspace.
    state.desk.available = false;
    state.desk.reason = error.status === 404
      ? "this console has no saved-desktop store yet"
      : (error.message || "the saved-desktop store could not be read");
    state.desk.version = 0;
    state.desk.raw = null;
    state.desk.desktops = [];
    state.desk.liveDesktops = [];
  }
  renderTabs();
  renderDesktopDialog();
}

/* One explicit change. The latest state is read first, the operation is applied
   to a copy of exactly that, and the write names the version it was based on.
   A conflict is never resolved by overwriting: the newer state is adopted so
   the person can see it, the local edit is kept, and Retry re-reads and
   re-applies rather than replaying a stale body. */
async function saveDesktopState(mutate, describe) {
  if (state.desk.saving) return {ok: false, message: "another change is still saving"};
  state.desk.saving = true;
  state.desk.conflict = null;
  try {
    const latest = await api(DESKTOP_PATH, {absolute: true});
    const next = JSON.parse(JSON.stringify((latest && latest.state) || {}));
    next.schema_version = next.schema_version || DESKTOP_SCHEMA;
    if (!Array.isArray(next.aliases)) next.aliases = [];
    if (!Array.isArray(next.desktops)) next.desktops = [];
    mutate(next);
    adoptDesktopState(await api(DESKTOP_PATH, {
      method: "PUT", absolute: true,
      body: {base_version: Number(latest && latest.version) || 0, state: next},
    }));
    return {ok: true};
  } catch (error) {
    if (error.code === "desktop_conflict") {
      // Somebody else's newer state is the truth now. Show it, keep what was
      // asked for, and let the person say whether to apply it on top.
      if (error.detail && error.detail.state) adoptDesktopState(error.detail);
      state.desk.conflict = {
        message: (describe || "That change") + " was not saved: your workspace changed "
          + "somewhere else. Nothing was overwritten.",
        retry: mutate, describe,
      };
      return {ok: false, conflict: true, message: state.desk.conflict.message};
    }
    if (error.status === 404) {
      state.desk.available = false;
      state.desk.reason = "this console has no saved-desktop store yet";
      return {ok: false, message: "kept on this device — there is no workspace store yet"};
    }
    return {ok: false, message: error.message || "the change could not be saved"};
  } finally {
    state.desk.saving = false;
    renderDesktopDialog();
    renderTabs();
  }
}

async function retryDesktopChange() {
  const held = state.desk.conflict;
  if (!held) return;
  state.desk.conflict = null;
  renderDesktopDialog();
  const result = await saveDesktopState(held.retry, held.describe);
  if (result.ok) flash((held.describe || "That change") + " is saved now.");
  else if (result.message) flash(result.message);
}

/* --------------------------------------------- one layout, on every device
   A person has one workspace, so by default they have one arrangement of it:
   the same conversations, in the same order, with the same one open, on the
   laptop, on the desktop and on the phone. That arrangement lives beside the
   saved desktops, under the same owner and the same compare-and-set, in its
   own versioned `sharedLayout` field.

   Four rules keep it honest.

   Nothing here touches a runtime. A tab that arrives from another device is
   drawn, not attached; a tab another device closed leaves this strip without
   a second release. Opening a conversation on purpose still attaches the
   ordinary way.

   Nothing here writes a picture it has not read. A device publishes the
   narrow thing the person just did — this set and order of tabs, this open
   conversation, this preference — replayed onto whatever the store hands
   back, so a tab another device added between the read and the write
   survives, and a tab it closed is not resurrected.

   Nothing here interrupts. A remote change to which conversation is open
   waits while there is a draft in the composer, a message going out, or a
   dialog on screen, and lands the next time this page is safely in front.

   And a device can leave. "Only this device" forks the arrangement here and
   stops both following and publishing; unchecking it rejoins whatever the
   workspace holds, without publishing the private one over it. */

const SCOPE_KEY = "atlas.layout-scope";
const LIVE_DESKTOP_SESSION_KEY = "atlas.live-desktop.session.1";
const LIVE_DESKTOP_HINT_KEY = "atlas.live-desktop.last.1";
const LIVE_DESKTOP_LEGACY_KEY = "atlas.live-desktop.1";
const LAYOUT_BACKUP_KEY = "atlas.tabs.before-shared";
const DEVICE_NAV_KEY = "atlas.nav.device.1";
const LAYOUT_SCHEMA = 1;
const LAYOUT_POLL_MS = 45000;
const LAYOUT_PUBLISH_MS = 700;

const shared = {
  applied: null,      // the layout this device last took from the store
  ops: [],            // narrow intents waiting to be published
  timer: 0,
  publishing: false,
  reading: false,
  poll: 0,
  applying: false,    // a change coming *from* the store; never published back
  deferred: null,     // a remote open-conversation change, held until it is safe
  lost: 0,            // tabs this device set aside when it joined the layout
  generation: 0,      // a late room read may never win after another desktop was joined
  selectedDesktopId: "", // this browser window's joined live desktop
};

function layoutScope() { return readPref(SCOPE_KEY) === "device" ? "device" : "shared"; }
function deviceOnlyLayout() { return layoutScope() === "device"; }

/* The selected live desktop is deliberately per browser window. Its members
   live in the owner store; the choice of which one a small phone or a second
   window is looking at does not. */
function selectedLiveDesktopId() {
  if (shared.selectedDesktopId) return shared.selectedDesktopId;
  let id = "";
  try { id = window.sessionStorage.getItem(LIVE_DESKTOP_SESSION_KEY) || ""; }
  catch (e) { /* use the last-used hint only to seed this window once */ }
  if (!id) {
    try {
      id = window.localStorage.getItem(LIVE_DESKTOP_HINT_KEY)
        || window.localStorage.getItem(LIVE_DESKTOP_LEGACY_KEY) || "";
    }
    catch (e) { /* private windows simply begin at the default */ }
  }
  shared.selectedDesktopId = id || "default";
  try { window.sessionStorage.setItem(LIVE_DESKTOP_SESSION_KEY, shared.selectedDesktopId); }
  catch (e) { /* the in-memory value is this window's fallback */ }
  return shared.selectedDesktopId;
}
function selectLiveDesktop(id) {
  shared.selectedDesktopId = id || "default";
  try { window.sessionStorage.setItem(LIVE_DESKTOP_SESSION_KEY, shared.selectedDesktopId); }
  catch (e) { /* the in-memory value is this window's fallback */ }
  // This is a startup hint for a newly opened window, never the current
  // window's source of truth. Existing windows keep their session value.
  try { window.localStorage.setItem(LIVE_DESKTOP_HINT_KEY, shared.selectedDesktopId); }
  catch (e) { /* private windows still keep the selection for this page */ }
}

function liveDesktops(raw) {
  const stored = raw && Array.isArray(raw.liveDesktops) ? raw.liveDesktops : null;
  if (stored) return stored.filter((desk) => desk && typeof desk === "object");
  // A previous console had one anonymous live layout. It becomes the stable
  // default on the first write; until then this read-only view loses nothing.
  const legacy = raw && raw.sharedLayout;
  return legacy && typeof legacy === "object"
    ? [{id: "default", name: "Shared desktop", layout: legacy}] : [];
}

function liveDesktop(raw, id) {
  const all = liveDesktops(raw);
  return all.find((desk) => String(desk.id || "") === (id || selectedLiveDesktopId())) || null;
}

/* The stored shape, believed only as far as it is readable. A layout written
   by a newer console is left strictly alone: not applied, not published over. */
function layoutRecord(raw) {
  const desk = liveDesktop(raw);
  const held = desk && desk.layout && typeof desk.layout === "object" ? desk.layout : null;
  if (!held || typeof held !== "object") return null;
  const version = Number(held.version) || 0;
  if (version > LAYOUT_SCHEMA) return {tooNew: true, tabs: [], active: null, customizations: {}};
  const configured = configuredIds();
  const tabs = [];
  const seen = new Set();
  for (const entry of Array.isArray(held.tabs) ? held.tabs : []) {
    const tab = cleanTab(entry, configured);
    if (!tab || seen.has(tabKey(tab.agent, tab.room))) continue;
    seen.add(tabKey(tab.agent, tab.room));
    tabs.push(tab);
  }
  const wanted = held.active && typeof held.active === "object" ? held.active : null;
  const room = wanted ? String(wanted.room || "") : "";
  return {
    tooNew: false, version, tabs, desktopId: String(desk.id || "default"),
    desktopName: String(desk.name || "Shared desktop"),
    active: ROOM_ID_SHAPE.test(room)
      ? {agent: String(wanted.agent || DEFAULT_AGENT), room} : null,
    customizations: held.customizations && typeof held.customizations === "object"
      ? held.customizations : {},
    by: String(held.updated_by || ""),
  };
}

function layoutKeys(tabs) { return tabs.map((tab) => tabKey(tab.agent, tab.room)); }

/* What this device would publish. Workspace aliases are already shared in
   their own field, so only a name given to this tab itself travels here. */
function sharedSnapshotTabs() {
  return state.tabs.map((tab) => {
    const record = {
      agent: tab.agent, room: tab.room,
      project: tab.project || "", title: tab.title || "",
      cap: tab.cap || "", identity: tab.identity || "",
      controllable: tab.controllable,
    };
    if (tab.customLabel) record.customLabel = tab.customLabel;
    return record;
  });
}

function activeRef() {
  const tab = state.tabs.find(isActiveTab);
  return tab ? {agent: tab.agent, room: tab.room} : null;
}

/* Metadata is only ever filled in: a stale layout must not blank a title this
   device just read from the runtime. */
function mergeSharedTab(held, incoming) {
  for (const field of ["project", "title", "cap", "identity"]) {
    if (!held[field] && incoming[field]) held[field] = incoming[field];
  }
  if (held.controllable === null && incoming.controllable !== null) {
    held.controllable = incoming.controllable;
  }
  held.unavailable = incoming.unavailable;
  // A layout only carries a name where it really differs from the workspace
  // alias; copying an identical one in would shadow the alias forever.
  const alias = state.desk.aliases.get(aliasKey(held.agent, held.room));
  if (incoming.customLabel && incoming.customLabel !== alias) {
    held.customLabel = incoming.customLabel;
  }
  return held;
}

/* Take the workspace's arrangement. On arrival that means adopting it whole,
   with this device's own strip kept as a backup rather than merged, because
   "the same everywhere" has to be true the moment you sign in. Afterwards it
   means applying only what actually changed. */
async function applySharedLayout(record, opts) {
  const first = Boolean(opts && opts.first);
  const previous = shared.applied;
  const remoteKeys = new Set(layoutKeys(record.tabs));
  shared.applying = true;
  try {
    let kept;
    if (first) {
      const lost = state.tabs.filter((tab) => !remoteKeys.has(tabKey(tab.agent, tab.room)));
      if (lost.length) {
        try {
          window.localStorage.setItem(LAYOUT_BACKUP_KEY,
            JSON.stringify({version: 2, at: Date.now(), tabs: state.tabs}));
        } catch (e) { /* private mode: there is simply no backup */ }
      }
      shared.lost = lost.length;
      kept = state.tabs.filter((tab) => remoteKeys.has(tabKey(tab.agent, tab.room)));
    } else {
      const before = new Set(previous ? layoutKeys(previous.tabs) : []);
      kept = state.tabs.filter((tab) => {
        const key = tabKey(tab.agent, tab.room);
        // Closed somewhere else: gone from this strip too, and nothing is
        // released for it here. Opened here since the last read: kept.
        return remoteKeys.has(key) || !before.has(key);
      });
    }
    const ordered = [];
    for (const incoming of record.tabs) {
      const held = kept.find((tab) => tab.agent === incoming.agent && tab.room === incoming.room);
      ordered.push(held ? mergeSharedTab(held, incoming) : incoming);
    }
    // Anything this device opened that the layout has not heard about yet
    // keeps its place at the end rather than disappearing under a poll.
    for (const tab of kept) {
      if (!remoteKeys.has(tabKey(tab.agent, tab.room))) ordered.push(tab);
    }
    state.tabs = ordered;
    writeTabs();
    applySharedCustomizations(record.customizations, previous && previous.customizations);
    renderTabs();
  } finally { shared.applying = false; }
  shared.applied = record;
  if (first && shared.lost) {
    // Said out loud, because a strip that quietly loses tabs is a strip nobody
    // trusts. Their sessions are untouched, and the old strip is still here.
    flash(shared.lost === 1
      ? "This is your shared layout. 1 conversation this device had open is not part of "
        + "it; nothing was closed or released."
      : "This is your shared layout. " + shared.lost + " conversations this device had "
        + "open are not part of it; nothing was closed or released.");
  }
  if (!first) void followSharedActive(record, previous);
}

/* Preferences, not pixels. A phone follows the theme and keeps its own
   geometry, which is the only geometry it has room for. */
function applySharedCustomizations(custom, previous) {
  const was = previous || {};
  const wanted = custom || {};
  if (wanted.theme && wanted.theme !== state.theme) setTheme(wanted.theme);
  if (!isWide()) return;
  if ((wanted.left === "rail" || wanted.left === "wide") && wanted.left !== state.ui.left) {
    setLeft(wanted.left);
  }
  if (typeof wanted.dock === "string" && wanted.dock !== was.dock
      && wanted.dock !== state.ui.dock) {
    if (wanted.dock) openPanel(wanted.dock);
    else if (state.ui.dock) closeDock(false);
  }
}

/* Which conversation is open travels too, but never over somebody's hands.
   What is protected is what is on screen and in flight: text in the composer,
   a message going out, a queued message being rewritten. A draft that is
   merely unsaved is not a reason to stay, because leaving a room flushes it
   before it goes anywhere. */
function safeToFollow() {
  if (document.hidden) return false;
  if (state.sending || state.pendingEdit) return false;
  if (state.conflict || attaching.size || state.roomRefreshing) return false;
  const draft = $("#draft");
  if (draft && draft.value.trim()) return false;
  const focused = document.activeElement;
  if (focused && (focused.tagName === "TEXTAREA"
      || (focused.tagName === "INPUT" && focused.type !== "checkbox"))) return false;
  if (document.querySelector("dialog[open]")) return false;
  return true;
}

async function followSharedActive(record, previous) {
  const want = record.active;
  if (!want) return;
  const was = previous && previous.active
    ? tabKey(previous.active.agent, previous.active.room) : "";
  if (tabKey(want.agent, want.room) === was) return;
  if (want.agent === agentId() && want.room === state.room) return;
  if (!configuredIds().has(want.agent)) return;
  shared.deferred = want;
  await followDeferredSession();
}

async function followDeferredSession() {
  const want = shared.deferred;
  if (!want || deviceOnlyLayout()) return;
  if (want.agent === agentId() && want.room === state.room) { shared.deferred = null; return; }
  if (!safeToFollow()) return;
  shared.deferred = null;
  shared.applying = true;
  // Following the layout is arranging, not choosing: it opens the
  // conversation and connects to nothing.
  try { await openSession(want.agent, want.room, {toTail: true, connect: false}); }
  finally { shared.applying = false; }
  renderTabs();
}

/* ------------------------------------------------------------- publishing */
function queueLayoutOp(op) {
  if (deviceOnlyLayout() || shared.applying) return;
  if (state.desk.available === false) return;
  // A device that has not read the workspace's layout cannot know what it
  // would be overwriting, so it does not write one. Seeding is deliberate.
  if (!shared.applied || shared.applied.tooNew) return;
  const desktopId = shared.applied.desktopId;
  // A delayed write belongs to the workspace the person was in when they
  // made it. Switching this window to another named desktop may not retarget
  // a tab move, close, or preference change.
  if (!desktopId || desktopId !== selectedLiveDesktopId()) return;
  shared.ops.push(Object.assign({desktopId}, op));
  clearTimeout(shared.timer);
  shared.timer = setTimeout(() => void flushLayoutOps(), LAYOUT_PUBLISH_MS);
}

/* The strip changed here. Metadata alone is not a change worth a write: a
   title arriving from a room read must not start a round of echoes. */
function publishTabs() {
  if (deviceOnlyLayout() || shared.applying) return;
  if (!shared.applied || shared.applied.tooNew) return;
  const base = layoutKeys(shared.applied.tabs);
  const tabs = sharedSnapshotTabs();
  if (layoutKeys(tabs).join("|") === base.join("|")) return;
  queueLayoutOp({kind: "tabs", tabs, base});
}

function publishActive() {
  if (deviceOnlyLayout() || shared.applying) return;
  if (!shared.applied || shared.applied.tooNew) return;
  const tab = activeRef();
  if (!tab) return;
  const held = shared.applied.active;
  if (held && held.agent === tab.agent && held.room === tab.room) return;
  queueLayoutOp({kind: "active", agent: tab.agent, room: tab.room});
}

function publishCustomizations() {
  if (deviceOnlyLayout() || shared.applying) return;
  if (!shared.applied || shared.applied.tooNew) return;
  const held = shared.applied.customizations || {};
  const wanted = {version: 1, theme: state.theme || "dark"};
  if (isWide()) {
    wanted.left = state.ui.left || "wide";
    wanted.dock = state.ui.dock || "";
  }
  if (Object.keys(wanted).every((key) => held[key] === wanted[key])) return;
  queueLayoutOp({kind: "custom", customizations: wanted});
}

function layoutFor(next, desktopId) {
  if (!Array.isArray(next.liveDesktops)) {
    const legacy = next.sharedLayout && typeof next.sharedLayout === "object"
      ? next.sharedLayout : {};
    next.liveDesktops = [{id: "default", name: "Shared desktop", layout: legacy}];
  }
  const id = desktopId || selectedLiveDesktopId();
  let desk = next.liveDesktops.find((entry) => entry && entry.id === id);
  if (!desk) {
    desk = {id, name: "Shared desktop", layout: {}};
    next.liveDesktops.push(desk);
  }
  const held = desk.layout && typeof desk.layout === "object" ? desk.layout : (desk.layout = {});
  if (!Array.isArray(held.tabs)) held.tabs = [];
  if (!held.customizations || typeof held.customizations !== "object") {
    held.customizations = {version: 1};
  }
  if (held.active !== null && typeof held.active !== "object") held.active = null;
  return held;
}

/* One intent, replayed onto whatever the store actually holds. This is the
   whole reason a conflict never costs anybody a tab: the write says what the
   person did, not what this browser believed the world looked like. */
function applyLayoutOp(layout, op) {
  const find = (agent, room) => layout.tabs.findIndex(
    (entry) => entry && String(entry.agent || DEFAULT_AGENT) === agent
      && String(entry.room || "") === room);
  if (op.kind === "tabs") {
    const base = new Set(op.base || []);
    const mine = new Set(layoutKeys(op.tabs));
    const rest = [];
    for (const entry of layout.tabs) {
      if (!entry || !entry.room) continue;
      const key = tabKey(String(entry.agent || DEFAULT_AGENT), String(entry.room));
      if (mine.has(key)) continue;
      // Only a tab this device actually had can have been closed by it. One
      // that arrived from somewhere else since the last read is not its to
      // remove, so it stays.
      if (base.has(key)) continue;
      rest.push(entry);
    }
    layout.tabs = op.tabs.map((tab) => {
      const at = find(tab.agent, tab.room);
      return at >= 0 ? Object.assign({}, layout.tabs[at], tab) : tab;
    }).concat(rest);
    return;
  }
  if (op.kind === "active") { layout.active = {agent: op.agent, room: op.room}; return; }
  if (op.kind === "custom") {
    layout.customizations = Object.assign({}, layout.customizations, op.customizations);
    return;
  }
  if (op.kind === "replace") {
    // Someone said, in as many words, that the layout is this one.
    layout.tabs = op.tabs;
    layout.active = op.active;
    layout.customizations = Object.assign({}, layout.customizations, op.customizations);
  }
}

function stampLayout(layout) {
  layout.version = LAYOUT_SCHEMA;
  layout.updated_by = state.device;
  layout.updated_at = new Date().toISOString();
  return layout;
}

function stampLiveLayout(next, layout, desktopId) {
  stampLayout(layout);
  // Keep the old field readable for consoles released before named desktops.
  // It is only the default desktop, so changing another named space can never
  // quietly replace what an older window is following.
  if ((desktopId || selectedLiveDesktopId()) === "default") next.sharedLayout = layout;
  return layout;
}

async function flushLayoutOps(retrying) {
  if (shared.publishing || deviceOnlyLayout() || !shared.ops.length) return;
  const desktopId = shared.ops[0].desktopId || selectedLiveDesktopId();
  const ops = shared.ops.filter((op) => (op.desktopId || desktopId) === desktopId);
  shared.ops = shared.ops.filter((op) => (op.desktopId || desktopId) !== desktopId);
  shared.publishing = true;
  let result;
  try {
    result = await saveDesktopState((next) => {
      const layout = layoutFor(next, desktopId);
      for (const op of ops) applyLayoutOp(layout, op);
      stampLiveLayout(next, layout, desktopId);
    }, "Your layout");
  } finally { shared.publishing = false; }
  if (result.ok) {
    if (selectedLiveDesktopId() === desktopId) {
      shared.applied = layoutRecord(state.desk.raw) || shared.applied;
    }
    if (shared.ops.length) {
      clearTimeout(shared.timer);
      shared.timer = setTimeout(() => void flushLayoutOps(), 0);
    }
    return;
  }
  if (result.conflict) {
    // Somebody else wrote between the read and the write. The newer state is
    // already adopted; the same narrow intent is simply replayed onto it, and
    // this is a background sync, so it never asks anyone to retry by hand.
    state.desk.conflict = null;
    shared.applied = layoutRecord(state.desk.raw) || shared.applied;
    shared.ops = ops.concat(shared.ops);
    if (!retrying) {
      clearTimeout(shared.timer);
      shared.timer = setTimeout(() => void flushLayoutOps(true), 250);
    }
    renderDesktopDialog();
    return;
  }
  if (result.message && /still saving/.test(result.message)) {
    shared.ops = ops.concat(shared.ops);
    clearTimeout(shared.timer);
    shared.timer = setTimeout(() => void flushLayoutOps(), 400);
  }
}

/* ---------------------------------------------------------------- reading */
/* Bounded and light: the store is read when this page comes back to the
   front, and on a slow timer while it is in front. A hidden page reads
   nothing, and a device with its own arrangement reads nothing at all. */
function scheduleLayoutPoll() {
  clearTimeout(shared.poll);
  if (document.hidden || state.desk.available === false) return;
  shared.poll = setTimeout(() => void refreshSharedLayout(), LAYOUT_POLL_MS);
}

async function refreshSharedLayout() {
  scheduleLayoutPoll();void checkDesktopDevice();
  if (deviceOnlyLayout() || document.hidden) return;
  if (shared.reading || shared.publishing || shared.ops.length || state.desk.saving) return;
  shared.reading = true;
  try {
    const envelope = await api(DESKTOP_PATH, {absolute: true});
    const version = Number(envelope && envelope.version) || 0;
    if (version === state.desk.version) { await followDeferredSession(); return; }
    adoptDesktopState(envelope);
    void renderRoomList();
    renderDesktopDialog();
    const record = layoutRecord(state.desk.raw);
    if (!record || record.tooNew) { renderTabs(); return; }
    if (record.by && record.by === state.device && shared.applied) {
      shared.applied = record;      // this device's own write, coming back
      renderTabs();
      return;
    }
    await applySharedLayout(record, {first: !shared.applied});
  } catch (error) {
    /* The store's own reads report whether it is there; a missed poll is not
       news, and nothing is applied from a read that did not happen. */
  } finally { shared.reading = false; }
}

/* ---------------------------------------------------- leaving and rejoining */
let deviceNavHeld = null;

/* The recent list is arranged for the workspace like everything else here, so
   a device that keeps its own layout keeps its own arrangement of it too. */
function deviceNav() {
  if (deviceNavHeld) return deviceNavHeld;
  let held = null;
  try { held = JSON.parse(window.localStorage.getItem(DEVICE_NAV_KEY) || "null"); }
  catch (e) { held = null; }
  deviceNavHeld = held && typeof held === "object" ? held : {};
  return deviceNavHeld;
}

function writeDeviceNav() {
  try { window.localStorage.setItem(DEVICE_NAV_KEY, JSON.stringify(deviceNav())); }
  catch (e) { /* private mode: the arrangement lasts this page */ }
}

async function setLayoutScope(deviceOnly) {
  if (Boolean(deviceOnly) === deviceOnlyLayout()) return;
  if (deviceOnly) {
    // Fork exactly what is on screen. Nothing is closed, nothing is released,
    // and the other devices keep the layout they already had.
    const raw = state.desk.raw && typeof state.desk.raw === "object" ? state.desk.raw : {};
    deviceNavHeld = JSON.parse(JSON.stringify({nav: raw.nav || {}}));
    writeDeviceNav();
    savePref(SCOPE_KEY, "device");
    shared.ops.length = 0;
    shared.deferred = null;
    clearTimeout(shared.timer);
    clearTimeout(shared.poll);
    writeTabs();
    flash("This device keeps its own arrangement now. Nothing was closed or released, "
      + "and your other devices keep the layout they had.");
  } else {
    savePref(SCOPE_KEY, "");
    deviceNavHeld = null;
    shared.applied = null;
    await loadDesktopState();
    const record = layoutRecord(state.desk.raw);
    if (record && !record.tooNew) await applySharedLayout(record, {first: true});
    scheduleLayoutPoll();
    flash(record && !record.tooNew
      ? "This device follows your shared layout again. Its own arrangement was kept as a "
        + "backup, and nothing was published over the shared one."
      : "This device follows your workspace again. There is no shared layout yet, so "
        + "nothing changed here and nothing was published.");
  }
  renderDesktopDialog();
  void renderRoomList();
  renderTabs();
}

/* The first layout is never guessed at. A browser that has just started, with
   whatever it happened to have open, must not become everybody's arrangement
   by accident, so this is a thing a person says. */
async function publishThisLayout() {
  if (deviceOnlyLayout() || state.desk.available === false) return {ok: false};
  const op = {kind: "replace", tabs: sharedSnapshotTabs(), active: activeRef(),
              customizations: desktopCustomizations(null)};
  const result = await saveDesktopState((next) => {
    const layout = layoutFor(next);
    applyLayoutOp(layout, op);
    stampLiveLayout(next, layout);
  }, "This layout");
  if (result.ok) {
    shared.applied = layoutRecord(state.desk.raw);
    scheduleLayoutPoll();
    flash("Your devices share this layout now. Nothing was attached or released.");
  } else if (result.message) flash(result.message);
  renderDesktopDialog();
  return result;
}

function nextLiveDesktopId() { return "live-" + desktopId().replace(/^d-/, ""); }

async function createLiveDesktop(name, snapshot, description = '') {
  if (state.desk.available === false) return {ok: false};
  const id = nextLiveDesktopId();
  const wanted = String(name || "").trim().slice(0, 80) || nextDesktopName();
  const source = snapshot || {tabs: sharedSnapshotTabs(), active: activeRef(),
                              customizations: desktopCustomizations(null)};
  const result = await saveDesktopState((next) => {
    // Materialise the old anonymous live layout once, retaining the legacy
    // field and every snapshot/owner extension beside it.
    if (!Array.isArray(next.liveDesktops)) {
      next.liveDesktops = next.sharedLayout && typeof next.sharedLayout === "object"
        ? [{id: "default", name: "Shared desktop", layout: next.sharedLayout}] : [];
    }
    next.liveDesktops.push({id, name: wanted, description:String(description).trim().slice(0,240),layout: stampLayout({version: LAYOUT_SCHEMA,
      tabs: source.tabs || [], active: source.active || null,
      customizations: source.customizations || {version: 1}})});
  }, "“" + wanted + "”");
  if (result.ok) {
    selectLiveDesktop(id);savePref(SCOPE_KEY,'');deviceNavHeld=null;
    shared.applied = layoutRecord(state.desk.raw);
    await applySharedLayout(shared.applied,{first:true});
    desktopChooser.selected=id;desktopChooser.manageRendered=false;
    shared.generation += 1;
    renderTabs(); scheduleLayoutPoll();
    flash("Created shared desktop “" + wanted + "”. Its tabs and order now follow this name.");
  } else {
    if (result.message) flash(result.message);
  }
  renderDesktopDialog();
  return result;
}

function hydrateLiveActive(record, generation) {
  const want = record && record.active;
  if (!want || !configuredIds().has(want.agent)) return;
  // Never make joining wait for a slow or unreachable agent. This only reads
  // the selected conversation; the strip was already drawn from saved data.
  void Promise.resolve().then(async () => {
    if (generation !== shared.generation || record.desktopId !== selectedLiveDesktopId()) return;
    await openSession(want.agent, want.room, {toTail: true, connect: false});
    if (generation !== shared.generation || record.desktopId !== selectedLiveDesktopId()) return;
    renderTabs();
  }).catch(() => { /* the visible cached strip remains useful */ });
}

async function joinLiveDesktop(id) {
  if (deviceOnlyLayout() || !id) return false;
  const previous = selectedLiveDesktopId();
  selectLiveDesktop(id);
  const generation = ++shared.generation;
  roomSelectionIntent += 1; // invalidate a room fetch started for the prior desktop
  shared.applied = null; shared.deferred = null;
  await loadDesktopState();
  const record = layoutRecord(state.desk.raw);
  if (!record || record.tooNew) {
    selectLiveDesktop(previous);
    flash("That shared desktop is no longer available.");
    return false;
  }
  await applySharedLayout(record, {first: true});
  hydrateLiveActive(record, generation);
  scheduleLayoutPoll(); renderDesktopDialog();
  flash("Joined shared desktop “" + record.desktopName + "”. Nothing was attached or released.");
  return true;
}

/* ---------------------------------------------------------------- the mark
   The logo opens the conversation about UX46 itself, which is the one place
   where saying what this space should be is the work. Which conversation that
   is belongs to the workspace, not to this build: nothing here knows a person,
   an agent or a room by name. */
function uiSessionRef() {
  const raw = state.desk.raw && typeof state.desk.raw === "object" ? state.desk.raw : null;
  const held = raw && raw.uiSession && typeof raw.uiSession === "object" ? raw.uiSession : null;
  if (!held) return null;
  const room = String(held.room || "");
  if (!ROOM_ID_SHAPE.test(room)) return null;
  return {agent: String(held.agent || DEFAULT_AGENT), room};
}

/* It opens that conversation the ordinary way, with the ordinary attach, and
   leaves every other tab exactly where it was. It writes no message and starts
   no session. */
async function openYourUx() {
  const want = uiSessionRef();
  if (!want) {
    flash("No conversation is designated as Your UX yet. Open the one you mean, then "
      + "choose it in Desktops.");
    openDesktopDialog();
    return false;
  }
  if (!configuredIds().has(want.agent)) {
    flash("Your UX is kept with " + agentLabel(want.agent)
      + ", which this console does not serve.");
    return false;
  }
  showView("console");
  dismissOverlay();
  return openTabForTyping(want.agent, want.room);
}

async function designateYourUx() {
  if (!state.room) { flash("Open the conversation you want first."); return; }
  const agent = agentId();
  const room = state.room;
  const result = await saveDesktopState((next) => { next.uiSession = {agent, room}; },
                                        "Your UX");
  if (result.ok) flash("The mark opens this conversation now, on every device.");
  else if (result.message) flash(result.message);
  renderDesktopDialog();
}

/* ----------------------------------------------------------- the controls */
function renderLayoutScope() {
  const tick = $("#deskLocalOnly");
  if (tick) {
    tick.checked = deviceOnlyLayout();
    tick.disabled = state.desk.available === false;
  }
  const note = $("#deskScopeNote");
  if (note) {
    note.textContent = deviceOnlyLayout()
      ? "This device keeps its own tabs, their order and its own recent list. It neither "
        + "follows your shared layout nor changes it. Conversations still use the same running agents."
      : "Devices joined to the same desktop share tabs, order and the selected conversation. "
        + "A different desktop changes your view, not the conversation’s connection.";
  }
  const host = $("#deskShared");
  if (!host) return;
  host.replaceChildren();
  if (state.desk.available === false) return;
  const record = layoutRecord(state.desk.raw);
  if (record && record.tooNew) {
    host.appendChild(el("p", {class: "desk-shared-note", text:
      "Your shared layout was written by a newer version of this console, so this one "
      + "leaves it alone rather than flattening what it cannot read."}));
  } else if (!record && !deviceOnlyLayout()) {
    host.appendChild(el("p", {class: "desk-shared-note", text:
      "Nothing is shared across your devices yet. This sends the strip as it is now: the "
      + "conversations, their order, and the one that is open."}));
    host.appendChild(el("button", {class: "ghost", type: "button",
      text: "Use this layout across devices",
      on: {click: () => void publishThisLayout()}}));
  } else if (record && !deviceOnlyLayout()) {
    host.appendChild(el("p", {class: "desk-shared-note", text:
      (record.tabs.length === 1 ? "1 conversation is"
        : record.tabs.length + " conversations are") + " shared across your devices."}));
  }

  const ux = uiSessionRef();
  host.appendChild(el("p", {class: "desk-shared-note", text: ux
    ? "The mark opens " + ux.room + " on " + agentLabel(ux.agent) + "."
    : "The mark has no conversation to open yet."}));
  const here = Boolean(state.room) && (!ux || ux.room !== state.room || ux.agent !== agentId());
  host.appendChild(el("button", {class: "ghost", id: "deskYourUx", type: "button",
    disabled: !here || state.desk.available === false,
    title: here ? "Point the mark at the conversation you have open"
      : "Open the conversation you want first",
    text: "Make this Your UX",
    on: {click: () => void designateYourUx()}}));
}

/* One page, one pair of listeners: coming back to this window is both the
   moment to ask what the workspace looks like now and the moment a change that
   waited for a clear composer can safely land. */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearTimeout(shared.poll); return; }
  void refreshSharedLayout();
});
window.addEventListener("focus", () => { void refreshSharedLayout(); });

/* ------------------------------------------------------------- tab labels */
/* What a tab is called. A name the person gave it wins, then the workspace's
   own alias for that session, then whatever the runtime called it. A canonical
   session is never renamed by any of this. */
function tabLabel(tab) {
  if (tab.customLabel) return tab.customLabel;
  const alias = state.desk.aliases.get(aliasKey(tab.agent, tab.room));
  if (alias) return alias;
  return tab.title || tab.room.split("/")[1] || tab.room;
}

function canonicalLabel(tab) {
  return tab.title || tab.room.split("/")[1] || tab.room;
}

function tabIsRenamed(tab) {
  return Boolean(tab.customLabel || state.desk.aliases.get(aliasKey(tab.agent, tab.room)));
}

/* Rename is a display alias for the workspace, not a session rename. An empty
   name puts the canonical one back and removes the alias. */
async function renameTab(tab, wanted) {
  const label = String(wanted || "").trim().slice(0, 60);
  const key = aliasKey(tab.agent, tab.room);
  // Applied here first, so the strip is right whether or not the store answers.
  if (label) {
    tab.customLabel = label;
    state.desk.aliases.set(key, label);
  } else {
    delete tab.customLabel;
    state.desk.aliases.delete(key);
  }
  writeTabs();
  renderTabs();
  if (state.desk.available === false) {
    flash(label ? "Renamed on this device — there is no workspace store yet."
                : "Name reset on this device — there is no workspace store yet.");
    return;
  }
  const agent = tab.agent;
  const room = tab.room;
  const result = await saveDesktopState((next) => {
    const at = next.aliases.findIndex((a) => a && a.agent === agent && a.room === room);
    if (!label) { if (at >= 0) next.aliases.splice(at, 1); return; }
    if (at >= 0) next.aliases[at] = Object.assign({}, next.aliases[at], {label});
    else next.aliases.push({agent, room, label});
  }, label ? "The new name" : "The name reset");
  if (result.ok) {
    // The workspace holds it now, so the device copy is redundant.
    delete tab.customLabel;
    writeTabs();
    renderTabs();
  } else if (result.message) flash(result.message);
}

/* -------------------------------------------------------------- reordering */
function moveTab(tab, delta) {
  const from = state.tabs.indexOf(tab);
  if (from < 0) return false;
  const to = Math.max(0, Math.min(state.tabs.length - 1, from + delta));
  if (to === from) return false;
  state.tabs.splice(from, 1);
  state.tabs.splice(to, 0, tab);
  writeTabs();
  renderTabs();
  const moved = $("#tabs").querySelector(
    '.tabwrap[data-agent="' + CSS.escape(tab.agent) + '"][data-room="'
    + CSS.escape(tab.room) + '"] .tab');
  if (moved) moved.focus();
  return true;
}

/* Dragging a tab moves it and nothing else: no request leaves, and the
   conversation on screen is untouched. A mouse starts after a few pixels; a
   finger has to hold still first, so a sideways flick still scrolls the strip
   instead of picking a tab up. */
const DRAG_HOLD_MS = 380;
const DRAG_SLOP = 6;
let tabDrag = null;

function clearDragMarks() {
  for (const node of $("#tabs").querySelectorAll(".tabwrap")) {
    node.classList.remove("dragging", "dropbefore", "dropafter");
  }
}

function dropTargetFor(x) {
  const wraps = [...$("#tabs").querySelectorAll(".tabwrap")];
  for (const wrap of wraps) {
    const box = wrap.getBoundingClientRect();
    if (x < box.left + box.width / 2) return {wrap, before: true};
  }
  const last = wraps[wraps.length - 1];
  return last ? {wrap: last, before: false} : null;
}

function beginTabDrag(wrap) {
  if (!tabDrag) return;
  tabDrag.active = true;
  wrap.classList.add("dragging");
  document.body.classList.add("tab-dragging");
  // Captured only once this really is a drag, so an ordinary click on a tab is
  // never diverted.
  try { wrap.setPointerCapture(tabDrag.pointer); } catch (e) { /* not captureable */ }
}

function endTabDrag(commit) {
  const held = tabDrag;
  tabDrag = null;
  document.body.classList.remove("tab-dragging");
  clearDragMarks();
  if (!held) return;
  clearTimeout(held.hold);
  // A press that never became a drag is a click. Redrawing here would replace
  // the node before its click even fired.
  if (!held.active) return;
  if (!commit || !held.target) { renderTabs(); return; }
  const tab = findTab(held.agent, held.room);
  const onto = findTab(held.target.wrap.dataset.agent, held.target.wrap.dataset.room);
  if (!tab || !onto || tab === onto) { renderTabs(); return; }
  const rest = state.tabs.filter((other) => other !== tab);
  let at = rest.indexOf(onto);
  if (!held.target.before) at += 1;
  rest.splice(at, 0, tab);
  state.tabs = rest;
  writeTabs();
  renderTabs();
}

function tabPointerDown(event, tab, wrap) {
  if (event.button !== undefined && event.button !== 0) return;
  if (tabDrag) endTabDrag(false);
  tabDrag = {
    agent: tab.agent, room: tab.room, wrap,
    x: event.clientX, y: event.clientY,
    touch: event.pointerType === "touch",
    active: false, moved: false, target: null, hold: 0,
    pointer: event.pointerId,
  };
  if (tabDrag.touch) {
    tabDrag.hold = setTimeout(() => {
      if (tabDrag && !tabDrag.moved) beginTabDrag(wrap);
    }, DRAG_HOLD_MS);
  }
}

function tabPointerMove(event) {
  if (!tabDrag) return;
  const dx = event.clientX - tabDrag.x;
  const dy = event.clientY - tabDrag.y;
  if (!tabDrag.active) {
    if (Math.abs(dx) > DRAG_SLOP || Math.abs(dy) > DRAG_SLOP) tabDrag.moved = true;
    // A finger that moved before the hold elapsed is scrolling the strip.
    if (tabDrag.touch) { if (tabDrag.moved) { clearTimeout(tabDrag.hold); } return; }
    if (!tabDrag.moved) return;
    beginTabDrag(tabDrag.wrap);
  }
  event.preventDefault();
  clearDragMarks();
  tabDrag.wrap.classList.add("dragging");
  const target = dropTargetFor(event.clientX);
  tabDrag.target = target;
  if (target) target.wrap.classList.add(target.before ? "dropbefore" : "dropafter");
}

/* -------------------------------------------------------------- tab menu */
let tabMenuFor = null;

function closeTabMenu(returnFocus) {
  const host = $("#tabMenu");
  if (!host || host.hidden) return;
  host.hidden = true;
  host.replaceChildren();
  const held = tabMenuFor;
  tabMenuFor = null;
  if (!returnFocus || !held) return;
  const back = $("#tabs").querySelector(
    '.tabwrap[data-agent="' + CSS.escape(held.agent) + '"][data-room="'
    + CSS.escape(held.room) + '"] .tab');
  if (back) back.focus();
}

function openTabMenu(tab, anchor) {
  const host = $("#tabMenu");
  if (!host) return;
  tabMenuFor = {agent: tab.agent, room: tab.room};
  host.replaceChildren();
  host.hidden = false;

  const name = tabLabel(tab);
  host.appendChild(el("p", {class: "tabmenu-head", text: name}));

  const form = el("form", {class: "tabmenu-rename", on: {submit: (event) => {
    event.preventDefault();
    const value = form.querySelector("input").value;
    closeTabMenu(true);
    void renameTab(tab, value);
  }}});
  const input = el("input", {type: "text", maxlength: "60", autocomplete: "off",
                             "aria-label": "Display name for this session",
                             placeholder: canonicalLabel(tab)});
  input.value = tabIsRenamed(tab) ? name : "";
  form.appendChild(input);
  form.appendChild(el("button", {class: "linkbtn", type: "submit", text: "Rename"}));
  host.appendChild(form);
  host.appendChild(el("p", {class: "tabmenu-note",
    text: state.desk.available === false
      ? "Kept on this device until the workspace store exists."
      : "A display name for your workspace. The session itself is not renamed."}));

  const acts = el("div", {class: "tabmenu-acts"});
  acts.appendChild(el("button", {class: "ghost", type: "button", text: "Next chapter…",
    on: {click: () => { closeTabMenu(false); void openEfficiency("chapter", tab); }}}));
  if (tabIsRenamed(tab)) {
    acts.appendChild(el("button", {class: "ghost", type: "button", text: "Reset name",
      on: {click: () => { closeTabMenu(true); void renameTab(tab, ""); }}}));
  }
  const at = state.tabs.indexOf(tab);
  acts.appendChild(el("button", {class: "ghost", type: "button", text: "Move left",
    disabled: at <= 0, on: {click: () => { closeTabMenu(false); moveTab(tab, -1); }}}));
  acts.appendChild(el("button", {class: "ghost", type: "button", text: "Move right",
    disabled: at < 0 || at >= state.tabs.length - 1,
    on: {click: () => { closeTabMenu(false); moveTab(tab, 1); }}}));
  host.appendChild(acts);

  const box = anchor.getBoundingClientRect();
  host.style.left = Math.max(8, Math.min(box.left, window.innerWidth - 300)) + "px";
  host.style.top = (box.bottom + 6) + "px";
  setTimeout(() => input.focus(), 0);
}

/* ---------------------------------------------------------- saved desktops */
function desktopSnapshotTabs() {
  return state.tabs.map((tab) => {
    const record = {
      agent: tab.agent, room: tab.room,
      project: tab.project || "", title: tab.title || "",
      cap: tab.cap || "", identity: tab.identity || "",
      controllable: tab.controllable,
    };
    // A name given here travels with the layout, and can differ from the
    // workspace alias for the same session.
    if (tab.customLabel) record.customLabel = tab.customLabel;
    else {
      const alias = state.desk.aliases.get(aliasKey(tab.agent, tab.room));
      if (alias) record.customLabel = alias;
    }
    return record;
  });
}

/* Only preferences this console actually has a hook for. A versioned container
   so a later one can add its own without this one dropping them. */
function desktopCustomizations(previous) {
  return Object.assign({}, previous || {}, {
    version: 1,
    theme: state.theme || "dark",
    dock: state.ui.dock || "",
    left: state.ui.left || "wide",
  });
}

function defaultDesktopName() {
  const active = state.tabs.find(isActiveTab) || state.tabs[0];
  const base = active ? tabLabel(active) : "Desktop";
  const others = Math.max(0, state.tabs.length - 1);
  return others ? base + " +" + others : base;
}

function nextDesktopName() {
  const names = new Set((state.desk.liveDesktops || []).map(d => d.name));
  let number = 1;
  while (names.has('Desktop ' + number)) number++;
  return 'Desktop ' + number;
}

function desktopId() {
  const raw = window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  return "d-" + raw.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 24);
}

async function saveDesktop(name, existingId) {
  const wanted = String(name || "").trim().slice(0, 80) || defaultDesktopName();
  if (state.desk.available === false) {
    flash("Desktops are kept for your workspace, and this console has no store for "
      + "them yet. Nothing was saved.");
    return {ok: false};
  }
  const tabs = desktopSnapshotTabs();
  const activeTab = state.tabs.find(isActiveTab);
  const active = activeTab ? {agent: activeTab.agent, room: activeTab.room} : null;
  const result = await saveDesktopState((next) => {
    const at = existingId ? next.desktops.findIndex((d) => d && d.id === existingId) : -1;
    if (at >= 0) {
      // Merge, so anything this console does not know about is left alone.
      const held = next.desktops[at];
      next.desktops[at] = Object.assign({}, held, {
        name: wanted, tabs, active,
        customizations: desktopCustomizations(held.customizations),
      });
      return;
    }
    next.desktops.push({
      id: desktopId(), name: wanted, tabs, active,
      customizations: desktopCustomizations(null),
    });
  }, "\u201c" + wanted + "\u201d");
  if (result.ok) flash("Saved “" + wanted + "”.");
  else if (result.message) flash(result.message);
  renderDesktopDialog();
  return result;
}

async function renameDesktop(id, name) {
  const wanted = String(name || "").trim().slice(0, 80);
  if (!wanted) return;
  const result = await saveDesktopState((next) => {
    const at = next.desktops.findIndex((d) => d && d.id === id);
    if (at >= 0) next.desktops[at] = Object.assign({}, next.desktops[at], {name: wanted});
  }, "That desktop's name");
  if (!result.ok && result.message) flash(result.message);
  renderDesktopDialog();
}

async function deleteDesktop(id) {
  const result = await saveDesktopState((next) => {
    const at = next.desktops.findIndex((d) => d && d.id === id);
    if (at >= 0) next.desktops.splice(at, 1);
  }, "That removal");
  if (!result.ok && result.message) flash(result.message);
  renderDesktopDialog();
}

/* Loading a desktop rearranges the strip. It does not attach, release, stop or
   prompt anything: conversations that drop off the strip keep running, and only
   the one this layout had open is opened. */
async function loadDesktop(id) {
  const saved = state.desk.desktops.find((d) => d && d.id === id);
  if (!saved) return false;
  const configured = configuredIds();
  const wanted = [];
  const seen = new Set();
  for (const raw of Array.isArray(saved.tabs) ? saved.tabs : []) {
    if (!raw || !raw.room || !ROOM_ID_SHAPE.test(String(raw.room))) continue;
    const agent = String(raw.agent || DEFAULT_AGENT);
    const key = tabKey(agent, raw.room);
    if (seen.has(key)) continue;
    seen.add(key);
    const held = findTab(agent, String(raw.room));
    const tab = held || {agent, room: String(raw.room), project: "", title: "",
                         cap: "", identity: "", controllable: null};
    if (raw.project) tab.project = String(raw.project);
    if (raw.title) tab.title = String(raw.title);
    if (raw.cap) tab.cap = String(raw.cap);
    if (raw.identity) tab.identity = String(raw.identity);
    if (raw.controllable !== undefined) tab.controllable = raw.controllable;
    // A layout only overrides a name where it really differs from the one the
    // workspace already holds. Copying an identical label in would shadow the
    // workspace alias forever, so a later change to it would never show.
    const label = raw.customLabel ? String(raw.customLabel) : "";
    const alias = state.desk.aliases.get(aliasKey(agent, String(raw.room)));
    if (label && label !== alias) tab.customLabel = label;
    else delete tab.customLabel;
    // A layout may name an agent this console no longer serves. That is worth
    // seeing and saying, not quietly dropping.
    tab.unavailable = configured.has(agent) ? "" : "this console does not serve " + agent;
    wanted.push(tab);
  }
  const hidden = state.tabs.filter((tab) => !seen.has(tabKey(tab.agent, tab.room)));
  state.tabs = wanted;
  writeTabs();

  const custom = saved.customizations || {};
  if (custom.theme) setTheme(custom.theme);
  if (custom.left === "rail" || custom.left === "wide") setLeft(custom.left);
  if (typeof custom.dock === "string") {
    if (custom.dock) openPanel(custom.dock);
    else if (state.ui.dock) closeDock(false);
  }
  renderTabs();

  const active = saved.active;
  const reachable = active && configured.has(String(active.agent || DEFAULT_AGENT));
  const generation = ++shared.generation;
  roomSelectionIntent += 1; // a later restore must win over its late room read
  if (active && reachable) {
    // The saved strip is useful immediately. A remote room can be slow or
    // unreachable, so it is read after the browser has painted and may not
    // block another saved tab or a later desktop choice.
    void Promise.resolve().then(async () => {
      if (generation !== shared.generation) return;
      await openSession(active.agent, active.room, {toTail: true, connect: false});
      if (generation === shared.generation) renderTabs();
    }).catch(() => {});
  } else if (state.room) {
    // Nothing from this layout could be opened, so the conversation you are
    // already in keeps its place. A strip that does not show where you are is
    // worse than one with an extra tab on it.
    openTab(agentId(), state.room, state.detail);
  }
  renderTabs();

  const notes = [];
  if (hidden.length) {
    notes.push(hidden.length === 1
      ? "1 other conversation was hidden from the strip; its session was left unchanged."
      : hidden.length + " other conversations were hidden from the strip; their sessions were "
        + "left unchanged.");
  }
  if (active && !reachable) {
    notes.push("Its open session belongs to an agent this console does not serve, so "
      + "nothing was opened.");
  }
  flash(["Restored saved layout “" + saved.name + "” into this desktop. Nothing was closed or released."]
    .concat(notes).join(" "));
  return true;
}

/* ------------------------------------------------------------ the dialog */
const desktopChooser = {window:clientId(), selected: '', busy: false, devices: null, checking: false, checkedAt: 0, error: '', managing: false, manageRendered: false};

function browserDeviceHint() {
  const ua = navigator.userAgent || '';
  const platform = /iPhone|iPod/.test(ua) ? 'iphone' : /iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1) ? 'ipad'
    : /Android/.test(ua) ? 'android' : /CrOS/.test(ua) ? 'chromeos' : /Windows/.test(ua) ? 'windows'
    : /Macintosh|Mac OS X/.test(ua) ? 'mac' : /Linux/.test(ua) ? 'linux' : 'other';
  const browser_kind = /Edg|EdgiOS/.test(ua) ? 'edge' : /Firefox|FxiOS/.test(ua) ? 'firefox'
    : /Chrome|CriOS/.test(ua) ? 'chrome' : /Safari/.test(ua) ? 'safari' : 'other';
  const names = {iphone:'iPhone',ipad:'iPad',android:'Android',chromeos:'Chromebook',windows:'Windows PC',mac:'Mac',linux:'Linux PC',other:'Device'};
  return {platform, browser_kind, name:names[platform], kind:['iphone','android'].includes(platform)?'phone':platform==='ipad'?'tablet':'computer'};
}

function desktopDeviceIcon(kind = 'computer') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 20 20');svg.setAttribute('class','desk-device-icon');svg.setAttribute('aria-hidden','true');
  const use = document.createElementNS('http://www.w3.org/2000/svg','use');
  use.setAttribute('href', kind === 'phone' || kind === 'tablet' ? '#i-device-phone' : kind === 'workspace' ? '#i-desktop-stack' : '#i-device-computer');
  svg.appendChild(use);return svg;
}

async function checkDesktopDevice(force = false) {
  if (!state.csrf || document.hidden || desktopChooser.checking || (!force && Date.now()-desktopChooser.checkedAt<40000)) return;
  desktopChooser.checking = true;desktopChooser.checkedAt = Date.now();
  try {
    const hint = browserDeviceHint();
    desktopChooser.devices = await api('/api/desktop-devices/check-in',{absolute:true,method:'POST',body:{browser:state.device,
      window:desktopChooser.window,desktop:selectedLiveDesktopId(),local:deviceOnlyLayout(),platform:hint.platform,browser_kind:hint.browser_kind}});
    desktopChooser.error = '';
  } catch (error) { desktopChooser.error = 'Device list unavailable. Your desktop selection still works.'; }
  finally {desktopChooser.checking = false;if(deviceOnlyLayout())scheduleLayoutPoll();renderDesktopChooser();}
}

function showDesktopManagement() {
  desktopChooser.managing = !desktopChooser.managing;desktopChooser.manageRendered = false;
  $('#deskManage').hidden = !desktopChooser.managing;
  $('#deskDialog').classList.toggle('managing',desktopChooser.managing);
  $('#deskManageToggle').setAttribute('aria-expanded',String(desktopChooser.managing));
  $('#deskManageToggle span').textContent = desktopChooser.managing ? 'Back to chooser' : 'Manage desktops';
  renderDesktopDialog();
  $('#deskDialog').scrollTop=0;
}

function deviceDesktopText(device) {
  const desks = state.desk.liveDesktops || [];
  const names = [...new Set((device.desktops || []).map(d => d.local ? 'a browser-only layout' : (desks.find(x => x.id===d.id)?.name || 'a desktop')))];
  return names.length ? (device.online ? 'Using ' : 'Last used ') + names.join(' · ') : 'No desktop reported yet';
}

function renderDesktopChooser() {
  const host = $('#deskLive');if (!host) return;
  const choices = state.desk.liveDesktops || [], data = desktopChooser.devices;
  const devices = (data?.devices || []).map(d=>({...d,online:Date.now()/1000-d.last_seen<100})), current = devices.find(d => d.id===data.current_device);
  const focusedCard=host.contains(document.activeElement)?document.activeElement.getAttribute('aria-label'):null;
  host.replaceChildren();
  for (const [index, item] of choices.entries()) {
    const selected = desktopChooser.selected === item.id;
    const using = devices.filter(d => d.online && d.desktops.some(x => !x.local && x.id===item.id));
    const secondary = item.id==='default' || index===0 ? 'Primary' : using.length===1 ? 'On '+using[0].name : using.length>1 ? using.length+' devices' : 'Available on any device';
    const tabs = item.layout?.tabs?.length || 0;
    const card = el('button',{class:'desk-card'+(selected?' selected':''),type:'button',role:'radio','aria-checked':String(selected),
      'aria-label':item.name,tabindex:selected || (!desktopChooser.selected && index===0)?'0':'-1',disabled:desktopChooser.busy || state.desk.available===false,
      on:{click:()=>{desktopChooser.selected=item.id;renderDesktopChooser();}}},[
      el('span',{class:'desk-card-top'},[desktopDeviceIcon('workspace'),el('span',{class:'desk-radio'})]),
      el('strong',{class:'desk-card-name',text:item.name}),el('span',{class:'desk-card-sub',text:secondary}),
      el('span',{class:'desk-card-description',text:item.description || (tabs+' '+(tabs===1?'conversation':'conversations')+'. Keep this workspace in sync wherever you choose it.')})]);
    host.appendChild(card);
  }
  if (deviceOnlyLayout()) {
    host.appendChild(el('button',{type:'button',role:'radio','aria-checked':String(desktopChooser.selected==='local'),
      class:'desk-card'+(desktopChooser.selected==='local'?' selected':''),'aria-label':'This browser only',
      tabindex:desktopChooser.selected==='local'?'0':'-1',disabled:desktopChooser.busy,
      on:{click:()=>{desktopChooser.selected='local';renderDesktopChooser();}}},[
      el('span',{class:'desk-card-top'},[desktopDeviceIcon(),el('span',{class:'desk-radio'})]),
      el('strong',{class:'desk-card-name',text:'This browser only'}),el('span',{class:'desk-card-description',text:'Your separate arrangement on this browser.'})]));
  }
  if (!choices.length && !deviceOnlyLayout()) host.appendChild(el('div',{class:'desk-empty'},[
    el('p',{text:'Create your first desktop from the conversations you have open.'}),
    el('button',{class:'primary',type:'button',text:'Create Desktop 1',on:{click:()=>void createLiveDesktop('Desktop 1')}})]));
  if(focusedCard)[...host.querySelectorAll('[role=radio]')].find(card=>card.getAttribute('aria-label')===focusedCard)?.focus({preventScroll:true});
  function row(device, here) {
    const row = el('div',{class:'desk-device-row'},[desktopDeviceIcon(device.kind),
      el('div',{class:'desk-device-main'},[el('strong',{text:device.name}),el('span',{text:deviceDesktopText(device)})]),
      here ? el('span',{class:'desk-device-pill',text:'This device'}) : el('span',{class:'desk-device-status'+(device.online?' online':''),
        text:device.online?'Online':device.last_seen?'Seen '+new Date(device.last_seen*1000).toLocaleDateString(): 'Not connected',
        title:device.last_seen?'Last UX46 check-in: '+new Date(device.last_seen*1000).toLocaleString():'No recent check-in'})]);
    if(here)row.appendChild(el('button',{class:'desk-device-edit',type:'button','aria-label':'Name or associate this device',text:'Edit',on:{click:()=>{
      if(!desktopChooser.managing)showDesktopManagement();$('#deskDeviceSettings input')?.focus();
    }}}));
    return row;
  }
  const fallback = {...browserDeviceHint(),name:browserDeviceHint().name,online:true,desktops:[{id:selectedLiveDesktopId(),local:deviceOnlyLayout()}]};
  $('#deskThisDevice').replaceChildren(row(current || fallback,true));
  const others = devices.filter(d=>d.id!==data?.current_device);
  $('#deskOtherDevices').replaceChildren(...others.map(d=>row(d,false)));
  if (!others.length) $('#deskOtherDevices').appendChild(el('p',{class:'desk-help',text:desktopChooser.error || 'Other devices appear when you open UX46 on them.'}));
  $('#deskDeviceNote').textContent=desktopChooser.error;$('#deskDeviceNote').hidden=!desktopChooser.error;
  $('#deskManageToggle').disabled=desktopChooser.busy;
  $('#deskApply').disabled = desktopChooser.busy || state.desk.available===false || !desktopChooser.selected;
  $('#deskApply').textContent = desktopChooser.busy ? 'Saving…' : 'Save';
  $('#deskCancel').disabled=desktopChooser.busy;$('#deskClose').disabled=desktopChooser.busy;
  if(desktopChooser.managing && !desktopChooser.manageRendered)renderDesktopManagement();
}

function renderDesktopManagement() {
  desktopChooser.manageRendered = true;
  const host = $('#deskManageLive');host.replaceChildren();
  for (const item of state.desk.liveDesktops || []) {
    const name = el('input',{type:'text',maxlength:80,'aria-label':'Rename '+item.name,value:item.name,required:true});
    const description = el('input',{type:'text',maxlength:240,'aria-label':'Description for '+item.name,value:item.description||'',placeholder:'What you use it for'});
    const form = el('form',{class:'desk-edit-form'},[name,description,el('button',{class:'linkbtn',type:'submit',text:'Update desktop'})]);
    form.addEventListener('submit',async event=>{event.preventDefault();const result=await saveDesktopState(next=>{
      layoutFor(next,item.id);const live=next.liveDesktops.find(d=>d.id===item.id);live.name=name.value.trim() || item.name;live.description=description.value.trim();
    },'Desktop details');if(result.ok){desktopChooser.manageRendered=false;renderDesktopDialog();}});host.appendChild(form);
  }
  const data=desktopChooser.devices,current=data?.devices.find(d=>d.id===data.current_device),settings=$('#deskDeviceSettings');settings.replaceChildren();
  if (!current) {settings.appendChild(el('p',{class:'desk-help',text:desktopChooser.error || 'Connecting this browser…'}));desktopChooser.manageRendered=false;return;}
  const name=el('input',{type:'text',maxlength:80,value:current.name,'aria-label':'Device name',required:true});
  const note=el('p',{class:'desk-help',role:'status',text:'This browser remembers the association. Use the same named device for its other browsers.'});
  const form=el('form',{class:'desk-edit-form'},[name,el('button',{class:'linkbtn',type:'submit',text:'Save device name'})]);
  const mutate=async(operation,body)=>{try{desktopChooser.devices=await api('/api/desktop-devices/'+operation,{absolute:true,method:'POST',body:{browser:state.device,...body}});
    desktopChooser.manageRendered=false;renderDesktopChooser();}catch(error){note.textContent=error.message;}};
  form.addEventListener('submit',event=>{event.preventDefault();void mutate('rename',{device:current.id,revision:current.revision,name:name.value});});
  settings.append(form,note);
  const others=data.devices.filter(d=>d.id!==current.id);
  if (others.length) {
    const select=el('select',{'aria-label':'Associate with an existing device'},[el('option',{value:'',text:'This is another browser on…'}),...others.map(d=>el('option',{value:d.id,text:d.name}))]);
    const bind=el('button',{type:'button',class:'linkbtn',text:'Associate browser',disabled:true,on:{click:()=>void mutate('associate',{device:select.value,previous:current.id})}});
    select.addEventListener('change',()=>{bind.disabled=!select.value;});settings.appendChild(el('div',{class:'desk-edit-form'},[select,bind]));
  }
}

async function applyDesktopChoice() {
  if(desktopChooser.busy)return;desktopChooser.busy=true;renderDesktopChooser();
  const choice=desktopChooser.selected,wasLocal=deviceOnlyLayout();let ok=true;
  try {
    if(choice==='local')await setLayoutScope(true);
    else if(choice!==selectedLiveDesktopId() || wasLocal) {
      if(wasLocal){savePref(SCOPE_KEY,'');deviceNavHeld=null;}
      ok=await joinLiveDesktop(choice);
      if(!ok && wasLocal)savePref(SCOPE_KEY,'device');
    }
    if(ok){await checkDesktopDevice(true);$('#deskDialog').close();}
  } catch(error) {$('#deskChoiceNote').textContent='Could not switch desktops. '+error.message;$('#deskChoiceNote').hidden=false;} finally {desktopChooser.busy=false;renderDesktopChooser();}
}

function renderDesktopDialog() {
  const list = $("#deskList");
  if (!list) return;
  const lede = $("#deskLede");
  if (lede) {
    lede.textContent = state.desk.available === false
      ? "Saved desktops live with your workspace. This console has no store for them "
        + "yet, so names and layouts are kept on this device only."
      : "Desktops are shared workspaces. Keep your tabs, order and open conversation in sync. Use the same desktop everywhere, or different ones on different devices.";
  }
  const note = $("#deskNote");
  if (note) {
    const held = state.desk.conflict;
    note.replaceChildren();
    note.hidden = !held;
    if (held) {
      note.appendChild(el("span", {text: held.message + " "}));
      note.appendChild(el("button", {class: "linkbtn", type: "button", text: "Try again",
        on: {click: () => retryDesktopChange()}}));
    }
  }
  const save = $("#deskSaveBtn");
  if (save) save.disabled = state.desk.saving || state.desk.available === false;
  const liveSave = $("#deskLiveBtn");
  if (liveSave) liveSave.disabled = state.desk.saving || state.desk.available === false;
  renderLayoutScope();

  renderDesktopChooser();

  list.replaceChildren();
  if (state.desk.available === false) return;
  if (!state.desk.desktops.length) {
    list.appendChild(el("p", {class: "desk-empty",
      text: "No saved layouts yet. Save a snapshot of the conversations you have open."}));
    return;
  }
  for (const saved of state.desk.desktops) {
    const row = el("div", {class: "desk-row", data: {desktop: saved.id}});
    const tabs = Array.isArray(saved.tabs) ? saved.tabs : [];
    const main = el("div", {class: "desk-main"});
    const naming = el("form", {class: "desk-rename", hidden: true,
      on: {submit: (event) => {
        event.preventDefault();
        void renameDesktop(saved.id, naming.querySelector("input").value);
      }}});
    const field = el("input", {type: "text", maxlength: "80", autocomplete: "off",
                               "aria-label": "Name for this desktop"});
    field.value = saved.name || "";
    naming.append(field, el("button", {class: "linkbtn", type: "submit", text: "Save"}));
    main.appendChild(el("button", {class: "desk-name", type: "button",
      title: "Rename this desktop", text: saved.name || "Untitled",
      on: {click: (event) => {
        event.currentTarget.hidden = true;
        naming.hidden = false;
        field.focus();
        field.select();
      }}}));
    main.appendChild(naming);
    main.appendChild(el("span", {class: "desk-sub",
      text: (tabs.length === 1 ? "1 conversation" : tabs.length + " conversations")
        + (saved.active ? " \u00b7 opens " + (saved.active.room || "") : "")}));
    row.appendChild(main);
    row.appendChild(el("button", {class: "linkbtn", type: "button", text: "Restore",
      title: "Restore this saved layout into the current desktop", on: {click: () => { $("#deskDialog").close(); void loadDesktop(saved.id); }}}));
    row.appendChild(el("button", {class: "ghost", type: "button", text: "Make shared",
      title: "Create a named live desktop from this saved layout",
      on: {click: () => void createLiveDesktop(saved.name, saved)}}));
    row.appendChild(el("button", {class: "ghost", type: "button", text: "Update",
      title: "Replace this desktop with the strip as it is now",
      on: {click: () => saveDesktop(saved.name, saved.id)}}));
    // Asked once rather than through a browser dialog, and it forgets the
    // question if it is left alone.
    const remove = el("button", {class: "ghost desk-delete", type: "button", text: "Delete",
      on: {click: () => {
        if (remove.dataset.armed) { void deleteDesktop(saved.id); return; }
        remove.dataset.armed = "1";
        remove.textContent = "Really delete?";
        setTimeout(() => {
          if (!remove.isConnected) return;
          delete remove.dataset.armed;
          remove.textContent = "Delete";
        }, 4000);
      }}});
    row.appendChild(remove);
    list.appendChild(row);
  }
}

/* ------------------------------------------------------ new conversation
   New, then who, then where. Three decisions, in that order, in one small
   dialog — and only the first two are required, because a conversation with
   nowhere to live is still a conversation worth having.

   It defaults to the configured default agent rather than whichever one you
   happen to be reading, because "new" should start from the same place every
   time rather than from wherever you drifted to. Browsing another agent's
   projects here reads that agent directly, through its own prefix; it never
   switches the console over to it, so cancelling leaves you exactly where you
   were, with the same tabs, the same reading position and the same drafts.

   Creating is one POST with a client id this dialog holds still. If the
   answer never arrives, the same id is what makes asking again safe: the
   request that may or may not have landed is the request that gets retried,
   never a second one alongside it. Changing the agent, the project or the
   name is a different request and gets a new id — and after an uncertain
   answer the form is locked until somebody says which of the two they meant,
   so nothing is created twice by editing a field. */

const newConv = {
  open: false,
  agent: DEFAULT_AGENT,
  options: null,          // what the chosen agent said it can do
  optionsFor: "",         // the agent that answer belongs to
  loading: false,
  reason: "",             // why this agent cannot start one here
  project: null,          // null is the blank exploration
  title: "",
  shape: "",              // agent + project + title behind the current id
  client: "",
  phase: "ready",         // ready, pending, uncertain
  note: "",
  tone: "",
  gen: 0,
  pushed: false,          // a history entry of our own, so Back closes this
  restore: null,
};

/* The id for exactly this request. It survives a retry of the same request
   and nothing else: change what is being asked for and it is a new ask. */
function newConvClient() {
  const shape = [newConv.agent, newConv.project === null ? "" : newConv.project,
                 newConv.title.trim()].join(" ");
  if (shape !== newConv.shape || !newConv.client) {
    newConv.shape = shape;
    newConv.client = clientId();
  }
  return newConv.client;
}

function newConvAgents() {
  const listed = state.agents || [];
  if (listed.length) return listed;
  return [{id: DEFAULT_AGENT, label: agentLabel(DEFAULT_AGENT), kind: "local"}];
}

function openNewConversation(anchor, preferredAgent, quick = false) {
  const dialog = $("#newDialog");
  if (!dialog || newConv.open) return;
  newConv.open = true;
  newConv.restore = anchor || document.activeElement;
  // Always the configured default, never wherever the reader happens to be.
  const known = newConvAgents();
  newConv.agent = known.some(a=>a.id===preferredAgent) ? preferredAgent : known.some((a) => a.id === DEFAULT_AGENT)
    ? DEFAULT_AGENT : known[0].id;
  newConv.project = null;
  newConv.title = "";
  newConv.shape = "";
  newConv.client = "";
  newConv.phase = "ready";
  newConv.note = "";
  newConv.tone = "";
  $("#newName").value = "";
  $("#newSearch").value = "";
  // On a phone this is a sheet, and Back is how a sheet is closed.
  try { window.history.pushState({ux46: "new"}, ""); newConv.pushed = true; }
  catch (e) { newConv.pushed = false; }
  renderNewAgents();
  const selected=newConv.agent;
  const loading=loadNewConvOptions();
  const generation=newConv.gen;
  if(quick)void loading.then(()=>{if(newConv.gen===generation&&newConv.open&&newConv.agent===selected&&newConv.project===null&&newConv.phase==="ready"&&newConv.options)void submitNewConversation();});
  if (!dialog.open) dialog.showModal();
  const first = $("#newAgents").querySelector(".nc-agent");
  if (first) first.focus();
}

function closeNewConversation(fromHistory) {
  const dialog = $("#newDialog");
  if (!newConv.open) return;
  newConv.open = false;
  newConv.gen += 1;                       // any answer still in flight is stale
  if (dialog && dialog.open) dialog.close();
  if (newConv.pushed && !fromHistory) {
    newConv.pushed = false;
    try { window.history.back(); } catch (e) { /* nothing to go back to */ }
  }
  if (fromHistory) newConv.pushed = false;
  const back = newConv.restore;
  newConv.restore = null;
  if (back && back.isConnected && back.focus) back.focus();
}

/* What one agent says it can do, read straight from that agent and nowhere
   else. A refusal is reported as a refusal: there is no second way to start a
   session behind this, and pretending otherwise would send a message into a
   conversation nobody asked for. */
async function loadNewConvOptions() {
  const agent = newConv.agent;
  const gen = ++newConv.gen;
  newConv.loading = true;
  newConv.reason = "";
  newConv.options = null;
  newConv.optionsFor = "";
  renderNewWhere();
  renderNewState();
  let options = null;
  let reason = "";
  try {
    options = await api(agentPath(agent, "/api/session-options"), {absolute: true});
  } catch (error) {
    reason = error.status === 404
      ? "The connection to " + agentLabel(agent) + " needs an update to support New conversation. Existing conversations are unchanged."
      : (error.message || "that agent could not be asked");
  }
  if (gen !== newConv.gen || !newConv.open) return;
  newConv.loading = false;
  if (options && options.available === false) {
    reason = options.message || agentLabel(agent) + " is not accepting new conversations.";
    options = null;
  }
  newConv.options = options;
  newConv.optionsFor = options ? agent : "";
  newConv.reason = reason;
  // A project that is no longer offered cannot stay chosen.
  const projects = (options && options.projects) || [];
  if (newConv.project !== null && !projects.some((p) => p.id === newConv.project)) {
    newConv.project = null;
  }
  renderNewWhere();
  renderNewState();
}

function setNewConvAgent(id) {
  if (newConv.phase !== "ready" || id === newConv.agent) return;
  newConv.agent = id;
  newConv.project = null;
  renderNewAgents();
  void loadNewConvOptions();
}

function renderNewAgents() {
  const host = $("#newAgents");
  if (!host) return;
  host.replaceChildren();
  for (const agent of newConvAgents()) {
    const about = agentAbout(agent);
    const chosen = agent.id === newConv.agent;
    host.appendChild(el("button", {
      class: "nc-agent" + (chosen ? " on" : ""), type: "button",
      "aria-pressed": String(chosen), "aria-label": about.label,
      title: about.label, disabled: newConv.phase !== "ready",
      data: {agent: agent.id},
      on: {click: () => setNewConvAgent(agent.id)},
    }, [agentAvatar(agent, {status: about.off ? "off" : "ok"}),
        el("span", {class: "nc-agent-name", text: agent.label})]));
  }
}

/* Blank first, because starting with nothing is a real choice and not a
   fallback, then whatever projects that agent offered. */
function renderNewWhere() {
  const host = $("#newList");
  if (!host) return;
  host.replaceChildren();
  const search = $("#newSearch");
  const options = newConv.options;
  const usable = Boolean(options) && !newConv.reason;
  if (search) search.disabled = !usable || newConv.phase !== "ready";
  $("#newWhereStep").hidden = !usable;
  $("#newSearch").hidden = !usable;
  if (!usable) return;

  const choose = (id) => {
    if (newConv.phase !== "ready") return;
    newConv.project = id;
    renderNewWhere();
    renderNewState();
  };
  const row = (id, name, note) => {
    const chosen = newConv.project === id;
    return el("button", {
      class: "nc-choice" + (chosen ? " on" : ""), type: "button",
      "aria-pressed": String(chosen), disabled: newConv.phase !== "ready",
      data: {project: id === null ? "" : id},
      on: {click: () => choose(id)},
    }, [
      el("span", {class: "nc-choice-name", text: name}),
      note ? el("span", {class: "nc-choice-note", text: note}) : null,
      chosen ? useIcon("i-check", "ic nc-tick") : null,
    ]);
  };

  if (options.blank !== false) {
    host.appendChild(row(null, "Blank exploration", "no project"));
  }
  const needle = (search ? search.value : "").trim().toLowerCase();
  const projects = (options.projects || []).filter((project) =>
    !needle || String(project.name || project.id).toLowerCase().includes(needle)
    || String(project.id).toLowerCase().includes(needle));
  for (const project of projects) {
    host.appendChild(row(project.id, project.name || project.id, ""));
  }
  if (!projects.length) {
    host.appendChild(el("p", {class: "nc-empty",
      text: needle ? "No project matches that."
        : agentLabel(newConv.agent) + " listed no projects. A blank one still works."}));
  }
}

/* The message, and which buttons are honest right now. */
function renderNewState() {
  const note = $("#newNote");
  const start = $("#newStart");
  const retry = $("#newRetry");
  const reset = $("#newReset");
  const name = $("#newName");
  if (!note || !start) return;

  const text = newConv.reason || newConv.note
    || (newConv.loading ? "Reading what " + agentLabel(newConv.agent) + " offers…" : "");
  note.textContent = text;
  note.className = "nc-note" + (newConv.reason || newConv.tone === "warn" ? " warn" : "");
  note.hidden = !text;

  const uncertain = newConv.phase === "uncertain";
  const pending = newConv.phase === "pending";
  const ready = Boolean(newConv.options) && !newConv.reason && !newConv.loading;
  start.hidden = uncertain;
  start.disabled = pending || !ready;
  start.textContent = pending ? "Starting…" : "Start conversation";
  retry.hidden = !uncertain;
  reset.hidden = !uncertain;
  name.disabled = !ready || pending || uncertain;
  $("#newCancel").disabled = pending;
}

function newConvBody() {
  return {
    client_id: newConvClient(),
    project_id: newConv.project,
    title: newConv.title.trim(),
  };
}

/* One create, on the agent that was chosen, addressed absolutely so a later
   change of mind in this dialog cannot re-point a request already in flight. */
async function submitNewConversation() {
  if (newConv.phase !== "ready" && newConv.phase !== "uncertain") return false;
  if (!newConv.options || newConv.reason) return false;
  const agent = newConv.agent;
  const gen = newConv.gen;
  const body = newConvBody();
  newConv.phase = "pending";
  newConv.note = "";
  newConv.tone = "";
  renderNewAgents();
  renderNewWhere();
  renderNewState();

  let result = null;
  let failure = "";
  let unknown = false;
  try {
    result = await api(agentPath(agent, "/api/sessions"),
                       {method: "POST", body, absolute: true});
  } catch (error) {
    // A refusal is an answer; anything else leaves the outcome unknown, and
    // an unknown outcome must never be quietly retried as a second request.
    if (error.status && error.status >= 400 && error.status < 500) {
      failure = error.message || "that request was refused";
    } else {
      unknown = true;
      failure = error.message || "the answer never arrived";
    }
  }
  if (gen !== newConv.gen || !newConv.open) return false;

  if (result && result.state === "created" && result.new_room && result.new_room.id) {
    return finishNewConversation(agent, result.new_room);
  }
  if (result && result.state === "uncertain") {
    unknown = true;
    failure = result.message || "the agent could not confirm whether it was created";
  }
  if (result && !unknown && !failure) {
    failure = result.message || "the agent did not say a conversation was created";
  }
  newConv.phase = unknown ? "uncertain" : "ready";
  newConv.tone = "warn";
  newConv.note = unknown
    ? failure + " It may or may not exist. Check and retry asks the same "
      + "question again rather than starting a second one."
    : failure;
  renderNewAgents();
  renderNewWhere();
  renderNewState();
  return false;
}

/* It exists. Give it a tab, go to it, and let the drawer catch up — in that
   order, so the tab is there even if the walk into the room is slow. */
async function finishNewConversation(agent, room) {
  openTab(agent, room.id, room);
  renderTabs();
  closeNewConversation();
  flash("Started " + (room.title || room.id) + " on " + agentLabel(agent) + ".");
  await openSession(agent, room.id, {toTail: true, connect: true});
  void loadWorkspace(true);
  return true;
}

/* Native chapters are new conversations; the workspace keeps the familiar
   seat, name and mark. Old conversations and saved layouts remain intact. */
async function finishChapter(agent, previousRoom, nextRoom, chapter, desktop, sourceTab, localOnly = deviceOnlyLayout()) {
  const next = typeof nextRoom === "string" ? {id: nextRoom} : nextRoom;
  const mode = chapter.mode || "replace";
  const oldTab = sourceTab || findTab(agent, previousRoom);
  const label = mode === "blank" ? (next.title || "New conversation") : (oldTab ? tabLabel(oldTab) : (next.title || previousRoom.split("/")[0]));
  const replacement = Object.assign({}, mode === "blank" ? {} : oldTab || {}, {
    agent, room: next.id, identity: next.identity_key || "", title: next.title || label,
    project: next.project_id || next.project || oldTab?.project || "", customLabel: label,
  });
  let canvasNote = "";
  if (mode !== "blank") {
    try {
      const canvasPath = (room) => "/api/boards/" + encodeURIComponent(agent) + "/" + encodeURI(room);
      const source = await api(canvasPath(previousRoom), {absolute: true});
      const destination = await api(canvasPath(next.id), {absolute: true});
      if (source.board && !destination.board) await api(canvasPath(next.id), {
        absolute: true, method: "PUT", body: {base_version: destination.version, board: source.board},
      });
    } catch (error) { canvasNote = " The new chapter exists, but its Canvas could not be carried forward: " + error.message; }
  }
  const result = await saveDesktopState((workspace) => {
    const matches = (entry, room) => entry && entry.agent === agent && entry.room === room;
    if (mode !== "blank") {
      if (!workspace.aliases.some((entry) => matches(entry, next.id))) workspace.aliases.push({agent, room: next.id, label});
      const mark = (workspace.sessionMarks || []).find((entry) => matches(entry, previousRoom));
      if (mark && !(workspace.sessionMarks || []).some((entry) => matches(entry, next.id))) {
        workspace.sessionMarks = [...(workspace.sessionMarks || []), {...mark, room: next.id}];
      }
      workspace.chapters = Array.isArray(workspace.chapters) ? workspace.chapters : [];
      if (!workspace.chapters.some((entry) => matches(entry, next.id))) workspace.chapters.push({
        agent, room: next.id, previous_room: previousRoom, label, at: new Date().toISOString(), mode,
      });
      workspace.chapters = workspace.chapters.slice(-100);
    }
    if (mode === "replace" && matches(workspace.uiSession, previousRoom)) workspace.uiSession = {agent, room: next.id};
    if (!localOnly) {
      const layout = layoutFor(workspace, desktop);
      if (mode === "replace") {
        layout.tabs = layout.tabs.filter((entry) => !matches(entry, next.id));
        const at = layout.tabs.findIndex((entry) => matches(entry, previousRoom));
        if (at >= 0) layout.tabs.splice(at, 1, replacement);
        else layout.tabs.push(replacement);
        if (matches(layout.active, previousRoom)) layout.active = {agent, room: next.id};
      } else if (!layout.tabs.some((entry) => matches(entry, next.id))) layout.tabs.push(replacement);
      stampLiveLayout(workspace, layout, desktop);
    }
  }, "Chapter layout");
  if (desktop === selectedLiveDesktopId()) {
    state.tabs = state.tabs.filter((tab) => !(tab.agent === agent && tab.room === next.id));
    const at = state.tabs.findIndex((tab) => tab.agent === agent && tab.room === previousRoom);
    if (mode === "replace" && at >= 0) state.tabs.splice(at, 1, replacement);
    else if (!findTab(agent, next.id)) state.tabs.push(replacement);
    // The owner write above already changed the shared layout. Avoid publishing
    // a second stale tabs operation over another device's concurrent changes.
    const prior = shared.applying; shared.applying = true;
    try { writeTabs(); renderTabs(); } finally { shared.applying = prior; }
  }
  if (!result.ok || canvasNote) flash((result.ok ? "Chapter created." : "Chapter created; " + result.message) + canvasNote);
  return next.id;
}

async function openEfficiency(mode = "usage", tab = null) {
  const agent = tab?.agent || agentId(), room = tab?.room || state.room;
  if (!window.UX46Efficiency) { flash("Refresh UX46 to load these controls."); return; }
  const label = tab ? tabLabel(tab) : (state.detail?.title || "This conversation");
  const parents = [];
  let child = room;
  for (let i = 0; i < 100; i++) {
    const parent = (state.desk.raw?.chapters || []).find((entry) => entry.agent === agent && entry.room === child);
    if (!parent || parents.some((entry) => entry.room === parent.previous_room)) break;
    parents.push({room: parent.previous_room, label: parent.label, at: parent.at}); child = parent.previous_room;
  }
  window.UX46Efficiency.open({mode, agent, room, label, agentLabel: agentLabel(agent), parents,
    api: (path) => api(path.startsWith("/api/skills") ? path : agentPath(agent, path), {absolute: true}),
    async start(command) {
      if (agent !== agentId() || room !== state.room) await openSession(agent, room, {connect: true});
      if (agent !== agentId() || room !== state.room) throw new Error("Could not open the selected conversation. No chapter was created.");
      return await sendCommand(command, false);
    },
    async prepare() {
      if (agent !== agentId() || room !== state.room) await openSession(agent, room, {connect: true});
      if (agent !== agentId() || room !== state.room) throw new Error("Could not open the selected conversation.");
      if (state.sending || state.roomRefreshing) throw new Error("This conversation is still sending or reconnecting. Try again when it finishes.");
      if ($("#draft").value.trim() || currentUploads().length) throw new Error("Your draft is kept. Send or clear it before preparing a handoff.");
      if (state.detail?.ownership?.state !== "atlas_owned") throw new Error("Connect this conversation before asking its agent to prepare a handoff.");
      const session = room.split("/")[1];
      $("#draft").value = "Prepare a compact handoff for the next UX46 chapter of " + room + ". "
        + "Use the context you already have. Write at most 1,800 characters to .ux46/continuations/" + session + ".md in this project's canonical home. "
        + "Include the current task, important recent decisions/results, next actions, exact relevant file/ledger references, and any unfinished worker or blocker. "
        + "Update the current Session Vault checkpoint once. Point to deeper material rather than copying transcripts or whole ledgers. "
        + "This is a handoff-only turn: do not start new implementation, deployment, sessions, subagents, or goals. Do not change the existing goal or its status. "
        + "Keep authority and pending actions explicit; a handoff grants no permission. The new chapter gets its own room identity; retain this one as history. "
        + "When saved, briefly confirm the path and stop so I can choose Next chapter.";
      resizeDraft();
      scheduleDraftSave();
      await send();
    },
    history: (previous) => openSession(agent, previous, {connect: false}),
  });
}

function resetNewConversation() {
  if (newConv.phase !== "uncertain") return;
  newConv.phase = "ready";
  newConv.note = "";
  newConv.tone = "";
  // A deliberate fresh start is a different request, and says so.
  newConv.shape = "";
  newConv.client = "";
  renderNewAgents();
  renderNewWhere();
  renderNewState();
}

/* Notes belong to the owner, independent of the selected agent or layout.
   Reuse the versioned owner store; every write reads latest and preserves all
   unrelated fields. Checking a box never invokes an agent. */
let notesSaving = false;
function renderNotes() {
  const host = $("#notesList"), doneHost = $("#notesDoneList");
  if (!host || !doneHost) return;
  const notes = Array.isArray(state.desk.raw?.notes) ? state.desk.raw.notes : [];
  host.replaceChildren(); doneHost.replaceChildren();
  for (const note of [...notes].reverse()) {
    if (!note || typeof note.text !== "string" || !note.id) continue;
    const checkbox = el("input", {type: "checkbox", "aria-label":
      (note.done ? "Reopen: " : "Complete: ") + note.text});
    checkbox.checked = !!note.done; checkbox.disabled = notesSaving;
    checkbox.addEventListener("change", () => changeNote(note.id, checkbox.checked));
    const row = el("label", {class: "notes-row" + (note.done ? " done" : "")},
      [checkbox, el("span", {text: note.text})]);
    (note.done ? doneHost : host).appendChild(row);
  }
  if (!host.childElementCount) host.appendChild(el("p", {class: "notes-empty", text: "Nothing waiting. Add a note whenever it comes to you."}));
  $("#notesCompleted").hidden = !doneHost.childElementCount;
  $("#notesCompletedTitle").textContent = "Completed · " + doneHost.childElementCount;
}
async function writeNote(mutate, description) {
  if (notesSaving) return false;
  notesSaving = true; renderNotes();
  $("#notesStatus").textContent = "Saving…";
  const result = await saveDesktopState(mutate, description);
  notesSaving = false; renderNotes();
  $("#notesStatus").textContent = result.ok ? "Saved" : result.message;
  return result.ok;
}
async function changeNote(id, done) {
  await writeNote(raw => {
    const note = (raw.notes || []).find(n => n.id === id);
    if (!note) throw new Error("This note no longer exists. Reopen Notes to refresh.");
    note.done = done; note.updated_at = new Date().toISOString();
  }, "That note");
}
async function refreshNotes() {
  try {
    adoptDesktopState(await api(DESKTOP_PATH, {absolute: true}));
    $("#notesStatus").textContent = "";
  } catch (_) { $("#notesStatus").textContent = "Could not refresh notes. Your saved notes are kept."; }
}
$("#notesForm").addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("#noteText"), text = input.value.trim();
  if (!text || notesSaving) return;
  const note = {id: clientId(), text, done: false, created_at: new Date().toISOString()};
  if (await writeNote(raw => {
    if (!Array.isArray(raw.notes)) raw.notes = [];
    raw.notes.push(note);
  }, "New note")) { if (input.value.trim() === text) input.value = ""; }
});
setInterval(() => {
  if (state.ui.dock === "notes" && !document.hidden && !notesSaving) void refreshNotes();
}, 15000);
window.addEventListener("focus", () => {
  if (state.ui.dock === "notes" && !notesSaving) void refreshNotes();
});

function openDesktopDialog() {
  if(desktopChooser.busy)return;
  const dialog = $("#deskDialog");
  if (!dialog) return;
  $('#deskChoiceNote').textContent='';$('#deskChoiceNote').hidden=true;
  $('#deskName').value = defaultDesktopName();$('#deskNewName').value=nextDesktopName();$('#deskNewDescription').value='';
  desktopChooser.selected=deviceOnlyLayout()?'local':selectedLiveDesktopId();desktopChooser.managing=false;desktopChooser.manageRendered=false;
  dialog.classList.remove('managing');
  $('#deskManage').hidden=true;$('#deskManageToggle').setAttribute('aria-expanded','false');$('#deskManageToggle span').textContent='Manage desktops';
  renderDesktopDialog();
  if (!dialog.open) dialog.showModal();
  void loadDesktopState();void checkDesktopDevice(true);
}

/* -------------------------------------------------------------- closing one
   Closing a tab is a release of exactly that agent's copy of that room. It is
   addressed to the tab's own agent, so the identically named room on another
   agent is untouched, and the tab only goes away once the release actually
   succeeded. Anything else keeps the tab and says why. */
const RELEASE_DONE = new Set(["released", "detached", "not_attached"]);

async function closeTab(tab) {
  if (tab.closing) return false;
  // Nothing to release: a room this agent said it cannot drive was never held.
  if (tab.controllable === false) {
    forgetTab(tab.agent, tab.room);
    renderTabs();
    return true;
  }
  tab.closing = true;
  tab.closeError = "";
  renderTabs();
  const label = agentLabel(tab.agent);
  const name = tab.title || tab.room;
  try {
    if (tab.agent === agentId() && tab.room === state.room) await flushDraft();
    const result = await api(
      agentPath(tab.agent, "/api/room/" + encodeURI(tab.room) + "/release"),
      {method: "POST", body: {}, absolute: true});
    const outcome = String(result.release_state || "");
    if (!RELEASE_DONE.has(outcome)) {
      // UX46 stopped the process but will not call the session free. Saying
      // the tab is gone would be claiming more than the runtime did.
      tab.closing = false;
      tab.closeError = result.message || "the release could not be confirmed";
      renderTabs();
      flash(name + " on " + label + ": " + tab.closeError);
      if (tab.agent === agentId() && tab.room === state.room) await refreshRoomState();
      return false;
    }
    const wasOpen = tab.agent === agentId() && tab.room === state.room;
    forgetTab(tab.agent, tab.room);
    if (wasOpen) {
      // Move to whatever place is left, preferring this agent's. With nowhere
      // to go, stay and re-read: the room is still there, it is simply not
      // held here any more, and Continue says so.
      const next = state.tabs.find((other) => other.agent === agentId()) || state.tabs[0];
      if (next) {
        // Forget where we were before leaving. Otherwise the walk to the next
        // place remembers this room on the way out and puts its tab straight
        // back — which is precisely what the person just closed.
        clearSpeech();
        state.room = null;
        state.detail = null;
        await openSession(next.agent, next.room, {toTail: true, connect: true});
      } else {
        await refreshRoomState();
      }
    }
    renderTabs();
    flash(result.message || ("Released " + name + " on " + label + "."));
    return true;
  } catch (error) {
    tab.closing = false;
    tab.closeError = error.message || "the release was refused";
    renderTabs();
    flash(name + " on " + label + ": " + tab.closeError + " — the tab is still here.");
    return false;
  }
}

/* The destination lives in the heading, so the composer below can be one
   field. Readable identity only: project, the Vault title, and which agent
   and capability answer for it. Native ids and paths stay in Connection
   details. */
function renderCrumb() {
  renderChapterNotice();
  const detail = state.detail;
  const crumb = $("#crumb");
  const cap = $("#sessCap");
  const kind = $("#sessKind");
  const project = $("#sessProject");
  crumb.replaceChildren();
  renderGoalBadge();
  const refresh = $("#btnSessionRefresh");
  refresh.hidden = !detail?.controllable;
  if (detail) refresh.title = "Refresh “" + (detail.title || detail.session) + "” on " + agentLabel()
    + ". An active turn will be preserved; no message is resent.";
  if (cap) cap.textContent = "";
  if (kind) kind.textContent = "";
  if (project) project.textContent = "";
  if (!detail) { crumb.textContent = "No room selected"; return; }
  // On a phone the project moves to the line below, so the session's own name
  // is never the part that gets truncated.
  if (project) project.textContent = detail.project_name;
  crumb.appendChild(el("b", {class: "project", text: detail.project_name}));
  crumb.appendChild(el("span", {class: "slash", "aria-hidden": "true", text: "/"}));
  crumb.appendChild(el("b", {text: detail.title || detail.session}));
  crumb.title = detail.id + ((detail.native || {}).thread_id
    ? " · native thread " + detail.native.thread_id : "");
  // Who answers is worth the room on any screen; the capability wording is
  // not, so the phone drops it and keeps the project instead.
  if (cap) {
    const owner = !detail.controllable && (state.agents || []).find(a =>
      a.runtime === detail.runtime && a.node === detail.node);
    cap.textContent = owner ? owner.label : (!detail.controllable && detail.runtime !== "codex"
      ? "Saved session" : agentLabel());
  }
  if (kind) kind.textContent = detail.capability_short || detail.capability_label || "";
}

// Chapter lineage is shared across desktops even when their tabs differ.
// Follow only explicit replacement links, never parallel branches or guesses
// based on titles. Old chapters remain readable without redirecting drafts.
function currentChapter(agent, room) {
  const seen = new Set([room]);
  let current = room;
  for (let n = 0; n < 100; n++) {
    const next = [...new Set((state.desk.raw?.chapters || []).filter(entry =>
      entry.agent === agent && entry.previous_room === current && entry.mode === "replace").map(entry => entry.room))];
    if (!next.length) return current === room ? null : current;
    if (next.length !== 1 || !next[0] || seen.has(next[0])) return null;
    current = next[0]; seen.add(current);
  }
  return null;
}
function renderChapterNotice() {
  const notice = $("#chapterNotice");
  if (!notice) return;
  const owner = agentId(), room = state.detail?.id;
  const next = room ? currentChapter(owner, room) : null;
  notice.hidden = !next;
  notice.replaceChildren();
  if (!next) return;
  notice.append(el("span", {text: "Earlier chapter · newer work is in the current chapter."}),
    el("button", {type: "button", class: "linkbtn", text: "Open current chapter",
      on: {click: () => openSession(owner, next, {connect: true, toTail: true})}}));
}

// Account availability is live evidence; native_terminal describes an earlier attempt.
function accountState(detail) { return detail?.account_status?.state || "unknown"; }
function accountSummary(detail) {
  const status = accountState(detail);
  if (status === "available") return "Account available";
  if (status === "limited") return "Usage limit reached";
  if (status === "sign_in_required") return "Sign-in required";
  return "Account availability unknown";
}
function terminalSummary(detail) {
  const terminal = detail?.native_terminal;
  if (!terminal || accountState(detail) === "available") return "";
  if (accountState(detail) === "limited") return "Usage limit reached";
  if (accountState(detail) === "sign_in_required") return "Sign-in required";
  return terminal.kind === "usage_limit" ? "Last attempt hit usage limit"
    : terminal.kind === "auth" ? "Last attempt had a sign-in error" : "Last attempt stopped before an answer";
}
function connectionSummary(detail) {
  const recovery = detail?.connection_recovery;
  const account = accountSummary(detail);
  if (recovery?.state === "refreshed") return "Connection refreshed · " + account.toLowerCase();
  if (recovery?.state === "deferred") return "Refresh deferred · " + (recovery.message || "active work preserved");
  if (recovery?.state === "failed") return "Connection refresh failed · " + (recovery.message || account.toLowerCase());
  return (state.connKind === "off" ? "Connection unavailable" : state.connKind === "live" ? "Connected" : "Checking connection") + " · " + account.toLowerCase();
}

function renderDetails() {
  const host = $("#connDetails");
  if (!host) return;
  const detail = state.detail;
  const summary = $("#connectionSummary");
  $("#settingsRoomName").textContent = detail ? (detail.title || detail.session) + " · " + agentLabel() : "Make this workspace feel right for you.";
  summary.textContent = detail ? connectionSummary(detail) : "No room selected";
  $("#settingsRefresh").disabled=!detail?.controllable;
  renderExecutionPolicyActual();
  $("#roomReleaseActions").replaceChildren();
  summary.dataset.status = detail ? accountState(detail) : "unknown";
  if (!detail) { host.textContent = "No room selected."; return; }
  const native = detail.native || {};
  const lines = [
    "room: " + detail.id,
    "title: " + (detail.title || detail.session),
    "capability: " + (detail.capability_label || ""),
    "ownership: " + ((detail.ownership || {}).state || "unknown"),
  ];
  if (native.thread_id) lines.push("native thread: " + native.thread_id);
  if (native.cwd) lines.push("working directory: " + native.cwd);
  if (native.model) lines.push("model: " + native.model);
  if (native.source) lines.push("started by: " + native.source);
  if (native.active_turn) lines.push("active turn: " + native.active_turn);
  if (detail.native_terminal) {
    lines.push("last terminal native turn: " + (detail.native_terminal.kind || "failed"));
    lines.push("status: " + (detail.native_terminal.status || "not reported"));
    lines.push("note: this records a previous native attempt, not current sign-in state");
  }
  // The composer no longer carries a permanent draft line, so the exact
  // saved state is always readable here instead.
  lines.push("draft: " + (state.draftDirty ? "not saved yet"
    : "saved · v" + (state.draft.version || 0)));
  if (state.historySource) lines.push("history read via: " + state.historySource);
  if (state.historyNote) lines.push("note: " + state.historyNote);
  if (detail.checkpoint) {
    lines.push("reported checkpoint: " + (detail.checkpoint.state || "?") + "/"
      + (detail.checkpoint.need || "?") + " by " + (detail.checkpoint.reporter || "?")
      + " at " + (detail.checkpoint.at || "?"));
  }
  if (detail.account_status?.checked_at) lines.push("account checked: " + detail.account_status.checked_at);
  if (detail.account_status?.reset_at) lines.push("usage resets: " + detail.account_status.reset_at);
  const technical = el("details", {class: "connection-technical"}, [
    el("summary", {text: "Technical details"}), el("div", {text: lines.join("\n")})]);
  host.replaceChildren(technical);
  if (detail.controllable) {
    if (detail.runtime === "codex") {
      technical.appendChild(el("p", {class: "command-note", text:
        "Account & connection · " + agentLabel() + ". To change accounts, run codex logout, then codex login on this agent’s host, using its normal OS account. Then choose Refresh all sessions from this agent’s menu, or refresh only this session."}));
      technical.appendChild(el("p", {class: "command-note", text:
        "At a usage limit? Open /usage in the Codex CLI to review the account’s available options. Refreshing does not reset usage or resend a prompt. On the UX46 gateway host, run ux46 doctor --agent " + agentId() + " --room " + detail.id + "."}));
    }
  }
  if (detail.controllable) {
    const owner = agentId();
    const connected = Boolean(detail.ownership?.atlas_owned);
    const busy = sessionConnectionActions.has(attachKey(owner, detail.id));
    const running = Boolean(native.active_turn || native.active_run || detail.approvals?.length);
    const elsewhere = detail.ownership?.state === "held_elsewhere";
    if (connected) $("#roomReleaseActions").appendChild(el("button", {
      class: "settings-release", type: "button", text: "Disconnect",
      disabled: busy || running,
      on: {click: () => changeSessionConnection(owner, detail.id, false)},
    }));
    $("#roomReleaseActions").appendChild(el("button", {
      class: "settings-action", type: "button", text: busy ? "Connecting…" : "Reconnect",
      disabled: busy || running || elsewhere,
      on: {click: () => changeSessionConnection(owner, detail.id, true)},
    }));
  }
}

/* What to say about when an entry happened. The runtime supplies a time for
   very little, so where it does not, this says where the entry is rather than
   showing a truncated id nobody can act on. The exact ids stay on the row's
   tooltip, in the source panel and in Connection details. */
function entryWhen(entry, loaded) {
  const at = entry.created_at || entry.timestamp || entry.at;
  if (typeof at === "number" && at > 0) return when(at);
  return loaded ? "In this session" : "Earlier in this session";
}

function itemHead(item) {
  const bits = [];
  if (item.turn_id) bits.push("turn " + shortId(item.turn_id));
  if (item.id) bits.push("item " + shortId(item.id));
  return bits.join(" · ");
}

function workTitle(item) {
  if (item.type === "subAgentActivity") {
    const labels = {started: "Subagent started", interacted: "Subagent interaction",
      interrupted: "Subagent interrupted", completed: "Subagent completed"};
    const label = labels[item.activity_kind] || "Subagent activity";
    return label + (item.agent_path ? " · " + item.agent_path : "");
  }
  if (item.type === "commandExecution") return item.command || "command";
  if (item.type === "fileChange") {
    const names = (item.changes || []).map((c) => c.path).filter(Boolean);
    return "file change · " + (names.length ? names.join(", ") : "no paths reported");
  }
  if (item.type === "mcpToolCall" || item.type === "dynamicToolCall") {
    return (item.server ? item.server + " · " : "") + (item.tool || "tool call");
  }
  if (item.type === "functionCallOutput") return "tool output · " + (item.name || "");
  if (item.type === "reasoning") return "reasoning summary";
  if (item.type === "webSearch") return "web search · " + (item.query || "");
  if (item.type === "plan") return "plan update";
  return item.type;
}

/* An empty reasoning item has nothing to show: an expandable row that opens
   on nothing is worse than no row. Non-empty summaries are kept verbatim. */
function emptyReasoning(item) {
  return item.type === "reasoning"
    && !(item.summary || []).some((line) => String(line).trim());
}

/* A recorded step reads as one line. The leading glyph says only what the
   runtime reported: a tick once the step reached a terminal status, a turning
   ring while it is still open. Nothing is inferred from its text. */
function workGlyph(item) {
  if (stepDone(item)) return useIcon("i-check", "wic");
  // A turning ring is a claim that this step is still running, so it is only
  // shown where the runtime actually reported a non-terminal status. An item
  // that reported nothing gets a mark that claims nothing.
  if (String(item.status || "").trim()) return el("span", {class: "spin", "aria-hidden": "true"});
  return el("span", {class: "wdot", "aria-hidden": "true"});
}

function workNode(item, groupKey) {
  const open = groupKey ? groupOpen(groupKey, item.turn_id) : workItemOpen(item);
  const wrap = el("div", {class: "work", data: {mid: item.id}});
  const tail = [];
  if (item.status) tail.push(item.status);
  if (item.exit_code !== undefined && item.exit_code !== null) tail.push("exit " + item.exit_code);
  if (item.duration_ms) tail.push(Math.round(item.duration_ms) + " ms");
  const toggle = el("button", {
    class: "work-toggle", type: "button", "aria-expanded": String(open),
    on: {click: () => {
      if (groupKey) state.openGroups[groupKey] = !open;
      else state.openWork[item.id] = !open;
      redrawKeepingPlace();
      renderActivity();
    }},
  }, [
    workGlyph(item),
    el("span", {class: "ttl", text: workTitle(item)}),
    el("span", {class: "tail", text: tail.join(" · ") || ""}),
    useIcon("i-chev", "chev"),
  ]);
  wrap.appendChild(toggle);
  if (open) {
    const body = el("div", {class: "work-body"});
    const meta = [];
    if (item.cwd) meta.push(item.cwd);
    if (meta.length) body.appendChild(el("p", {class: "meta", text: meta.join(" · ")}));
    body.title = itemHead(item);
    if (item.type === "reasoning") {
      for (const line of item.summary || []) body.appendChild(el("p", {class: "meta", text: line}));
    }
    if (item.type === "fileChange") {
      for (const change of item.changes || []) {
        body.appendChild(el("p", {class: "meta", text: (change.kind || "change") + " · " + change.path}));
      }
    }
    const output = item.output || "";
    if (output) body.appendChild(el("pre", {class: "out", text: output}));
    if (!output && item.type === "commandExecution") {
      body.appendChild(el("p", {class: "meta", text: "the runtime reported no output for this command"}));
    }
    if (item.type === "subAgentActivity") {
      if (item.agent_path) body.appendChild(el("p", {class: "meta", text: "Agent: " + item.agent_path}));
      body.appendChild(el("p", {class: "meta", text: item.agent_thread_id
        ? "This records an event in the agent’s work. Its task, steps and answer belong to its own conversation and aren’t included in this event."
        : "This connection didn’t provide the agent details. The console adapter needs the subagent display update."}));
      if (item.agent_thread_id) body.appendChild(el("details", {}, [
        el("summary", {text: "Conversation reference"}),
        el("p", {class: "meta", text: item.agent_thread_id})]));
    } else if (item.raw_keys) {
      body.appendChild(el("p", {class: "meta",
        text: "this runtime item type is not specially rendered · fields: " + item.raw_keys.join(", ")}));
    }
    wrap.appendChild(body);
  }
  return wrap;
}

/* ----------------------------------------------------------- agent markdown
   A bounded renderer for agent replies. Every node is made with
   createElement and filled with textContent: no innerHTML, no HTML parsing,
   no images fetched, no javascript:/data:/file: navigation. Syntax it does
   not understand stays literal, so a half-streamed reply reads as what the
   runtime actually sent. item.text is never rewritten — the exact source
   stays in state and behind the per-message "source" toggle. */

const MD_MAX_DEPTH = 4;
const MD_MAX_CHARS = 200000;
const MD_WEB_URL = /^https?:\/\/[^\s<>"'`]+$/i;

function mdBlank(line) { return !line.trim(); }
function mdHeading(line) { return /^ {0,3}(#{1,6})[ \t]+(.*)$/.exec(line); }
function mdFenceOpen(line) { return /^ {0,3}(`{3,}|~{3,})[ \t]*([^`]*)$/.exec(line); }
function mdFenceClose(line) { return /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(line); }
function mdRule(line) { return /^ {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$/.test(line); }
function mdQuote(line) { return /^ {0,3}>/.test(line); }
function mdBullet(line) { return /^( {0,7})([-*+])[ \t]+(.*)$/.exec(line); }
function mdOrdered(line) { return /^( {0,7})(\d{1,9})[.)][ \t]+(.*)$/.exec(line); }
function mdDivider(line) {
  return /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$/.test(line);
}

/* Does this line open a different block than the paragraph being collected? */
function mdBreaksParagraph(line) {
  if (mdBlank(line)) return true;
  if (mdHeading(line) || mdFenceOpen(line) || mdQuote(line)) return true;
  if (mdRule(line)) return true;
  return Boolean(mdBullet(line) || mdOrdered(line));
}

/* Split one table row on unescaped pipes. */
function mdCells(line) {
  let text = line.trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|") && !text.endsWith("\\|")) text = text.slice(0, -1);
  const cells = [];
  let buf = "";
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "\\" && text[i + 1] === "|") { buf += "|"; i += 1; continue; }
    if (ch === "|") { cells.push(buf.trim()); buf = ""; continue; }
    buf += ch;
  }
  cells.push(buf.trim());
  return cells;
}

function mdAlign(spec) {
  const cell = spec.trim();
  if (cell.startsWith(":") && cell.endsWith(":")) return "ta-c";
  if (cell.endsWith(":")) return "ta-r";
  return "";
}

/* ------------------------------------------------------------ destinations */
/* Only http(s) may become a real link. A local path is shown as a file
   reference with the path itself visible and selectable; anything else is
   shown as plain text. Nothing else can navigate. */
function mdDestKind(dest) {
  const raw = String(dest || "").trim();
  if (!raw) return "none";
  if (MD_WEB_URL.test(raw)) return "web";
  if (raw.startsWith("//")) return "other";
  if (/^(?:\/|~\/|\.{1,2}\/|[A-Za-z]:[\\/])/.test(raw) || /^file:/i.test(raw)) return "path";
  return "other";
}

function mdRefNode(label, dest, kind, depth) {
  const path = kind === "path";
  const wrap = el("span", {
    class: "mdref" + (path ? " path" : ""),
    title: path
      ? "local path on this machine — UX46 does not open files, the path is shown so you can copy it"
      : "not an http(s) link — shown as text, not opened",
  });
  const text = String(label || "").trim();
  if (text) {
    const name = el("span", {class: "rl"});
    name.appendChild(mdInline(text, depth + 1));
    wrap.appendChild(name);
  }
  if (dest) {
    wrap.appendChild(el("span", {class: "sr-only",
      text: path ? " local path, not a link: " : " not a link: "}));
    wrap.appendChild(el("code", {class: "rp", text: dest}));
  }
  return wrap;
}

function mdLinkNode(label, dest, depth) {
  const kind = mdDestKind(dest);
  if (kind !== "web") return mdRefNode(label, dest, kind, depth);
  const link = el("a", {
    class: "mdlink", href: dest, title: dest,
    target: "_blank", rel: "noopener noreferrer nofollow",
  });
  const text = String(label || "").trim();
  // A bare or auto link labels itself: keep it plain, never re-scan the URL.
  if (!text || text === dest) link.appendChild(document.createTextNode(dest));
  else link.appendChild(mdInline(text, depth + 1));
  link.appendChild(el("span", {class: "ext", "aria-hidden": "true", text: "↗"}));
  return link;
}

/* ------------------------------------------------------------ inline scans */
function mdBracket(src, at) {
  let depth = 0;
  for (let i = at; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === "\\") { i += 1; continue; }
    if (ch === "[") depth += 1;
    else if (ch === "]") {
      depth -= 1;
      if (!depth) return {text: src.slice(at + 1, i), end: i + 1};
    }
  }
  return null;
}

function mdParen(src, at) {
  let depth = 0;
  for (let i = at; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === "\\") { i += 1; continue; }
    if (ch === "(") depth += 1;
    else if (ch === ")") {
      depth -= 1;
      if (!depth) {
        let raw = src.slice(at + 1, i).trim();
        const titled = /^(.*?)[ \t]+(?:"([^"]*)"|'([^']*)')$/.exec(raw);
        if (titled) raw = titled[1].trim();
        if (raw.startsWith("<") && raw.endsWith(">")) raw = raw.slice(1, -1).trim();
        return {dest: raw, end: i + 1};
      }
    }
  }
  return null;
}

function mdCodeSpan(src, at) {
  const open = /^`+/.exec(src.slice(at))[0];
  let i = at + open.length;
  while (i < src.length) {
    if (src[i] !== "`") { i += 1; continue; }
    const run = /^`+/.exec(src.slice(i))[0];
    if (run.length !== open.length) { i += run.length; continue; }
    let body = src.slice(at + open.length, i).replace(/\n/g, " ");
    if (body.length > 2 && body.startsWith(" ") && body.endsWith(" ") && body.trim()) {
      body = body.slice(1, -1);
    }
    if (!body) return null;
    return {node: el("code", {class: "mdspan", text: body}), end: i + run.length};
  }
  return null;
}

function mdEmphasis(src, at, depth) {
  const ch = src[at];
  const double = src[at + 1] === ch;
  if (ch === "~" && !double) return null;
  const marker = double ? ch + ch : ch;
  if (/[\s]/.test(src[at + marker.length] || " ")) return null;
  if (ch === "_" && /[A-Za-z0-9]/.test(src[at - 1] || " ")) return null;
  let from = at + marker.length;
  while (from < src.length) {
    const hit = src.indexOf(marker, from);
    if (hit < 0) return null;
    const body = src.slice(at + marker.length, hit);
    if (!body.trim() || /\s$/.test(body)) { from = hit + marker.length; continue; }
    if (ch === "_" && /[A-Za-z0-9]/.test(src[hit + marker.length] || " ")) {
      from = hit + marker.length;
      continue;
    }
    const tag = double ? (ch === "~" ? "s" : "strong") : "em";
    const node = el(tag, null);
    node.appendChild(mdInline(body, depth + 1));
    return {node, end: hit + marker.length};
  }
  return null;
}

function mdAutolink(src, at) {
  const end = src.indexOf(">", at + 1);
  if (end < 0) return null;
  const body = src.slice(at + 1, end);
  if (!MD_WEB_URL.test(body)) return null;   // an HTML-looking tag stays literal text
  return {node: mdLinkNode(body, body, MD_MAX_DEPTH), end: end + 1};
}

function mdBareUrl(src, at) {
  const match = /^https?:\/\/[^\s<>"'`]+/i.exec(src.slice(at));
  if (!match) return null;
  let url = match[0];
  while (url.length > 8) {
    const last = url[url.length - 1];
    if (".,;:!?".includes(last)) { url = url.slice(0, -1); continue; }
    if ((last === ")" && !url.includes("(")) || (last === "]" && !url.includes("["))) {
      url = url.slice(0, -1);
      continue;
    }
    break;
  }
  if (!MD_WEB_URL.test(url)) return null;
  return {node: mdLinkNode(url, url, MD_MAX_DEPTH), end: at + url.length};
}

/* An image is never fetched: it becomes a labelled reference to its source. */
function mdImageRef(src, at, depth) {
  const label = mdBracket(src, at + 1);
  if (!label) return null;
  if (src[label.end] !== "(") return null;
  const dest = mdParen(src, label.end);
  if (!dest) return null;
  const wrap = el("span", {class: "mdref image", title: "image reference — UX46 does not load images from a transcript"});
  wrap.appendChild(el("span", {class: "rk", text: "image"}));
  const text = label.text.trim();
  if (text) {
    const name = el("span", {class: "rl"});
    name.appendChild(mdInline(text, depth + 1));
    wrap.appendChild(name);
  }
  if (dest.dest) wrap.appendChild(el("code", {class: "rp", text: dest.dest}));
  return {node: wrap, end: dest.end};
}

function mdVisualization(src, at) {
  const prefix = '\uE200visualize\uE202';
  if (!src.startsWith(prefix, at)) return null;
  const end = src.indexOf('\uE201', at + prefix.length);
  if (end < 0 || end - at > 4096) return null;
  const wrap = el('span', {class: 'mdvisualization'});
  try {
    const ref = JSON.parse(src.slice(at + prefix.length, end));
    if (!ref || typeof ref.path !== 'string' || !ref.path.startsWith('/') || !ref.path.endsWith('.html')) throw new Error('Invalid reference');
    const title = ref.path.split('/').pop().replace(/\.html$/, '').replace(/[-_]/g, ' ');
    const url = '/api/visualizations/render?path=' + encodeURIComponent(ref.path);
    wrap.appendChild(el('iframe', {src: url, title, sandbox: 'allow-scripts', loading: 'lazy', referrerpolicy: 'no-referrer'}));
    wrap.appendChild(el('a', {href: url, target: '_blank', rel: 'noopener noreferrer', text: 'Open visualization ↗'}));
  } catch (_) {
    wrap.appendChild(el('span', {text: 'This visualization reference is incomplete or unavailable.'}));
  }
  return {node: wrap, end: end + 1};
}

function mdInline(src, depth) {
  const frag = document.createDocumentFragment();
  const text = String(src == null ? "" : src);
  let buf = "";
  const flush = () => {
    if (!buf) return;
    const parts = buf.split("\n");
    parts.forEach((part, index) => {
      if (index) frag.appendChild(document.createElement("br"));
      if (part) frag.appendChild(document.createTextNode(part));
    });
    buf = "";
  };
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    let taken = null;
    if (ch === '\uE200') taken = mdVisualization(text, i);
    else if (ch === "`") taken = mdCodeSpan(text, i);
    else if (ch === "!" && text[i + 1] === "[" && depth < MD_MAX_DEPTH) taken = mdImageRef(text, i, depth);
    else if (ch === "[" && depth < MD_MAX_DEPTH) {
      const label = mdBracket(text, i);
      if (label && text[label.end] === "(") {
        const dest = mdParen(text, label.end);
        if (dest) taken = {node: mdLinkNode(label.text, dest.dest, depth), end: dest.end};
      }
    } else if (ch === "<" && depth < MD_MAX_DEPTH) taken = mdAutolink(text, i);
    else if ((ch === "*" || ch === "_" || ch === "~") && depth < MD_MAX_DEPTH) taken = mdEmphasis(text, i, depth);
    else if ((ch === "h" || ch === "H") && depth < MD_MAX_DEPTH
             && (i === 0 || /[\s(<[]/.test(text[i - 1]))) taken = mdBareUrl(text, i);
    if (taken) { flush(); frag.appendChild(taken.node); i = taken.end; continue; }
    buf += ch;
    i += 1;
  }
  flush();
  return frag;
}

/* -------------------------------------------------------------- block scan */
/* Local reply utilities: no model calls and no transcript writes. */
function replyIcon(kind) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [key, value] of Object.entries({viewBox: "0 0 24 24", width: "17", height: "17", fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true"})) svg.setAttribute(key, value);
  const shapes = {
    copy: "M9 9h11v12H9z M15 9V3H3v12h6",
    download: "M12 3v12m-5-5 5 5 5-5 M4 16v5h16v-5",
    play: "m8 4 12 8-12 8z",
    pause: "M8 5v14 M16 5v14",
    listen: "M11 4 6 8H3v8h3l5 4z M15 8a6 6 0 0 1 0 8 M18 5a10 10 0 0 1 0 14",
    check: "m5 12 4 4 10-10",
  };
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", shapes[kind] || shapes.copy); svg.appendChild(path);
  return svg;
}
function replyAction(kind, label, onClick) {
  return el("button", {class: "reply-action", type: "button", title: label,
    "aria-label": label, on: {click: onClick}}, [replyIcon(kind)]);
}
async function copyReplyText(text, button, label) {
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("Clipboard is unavailable in this browser");
    await navigator.clipboard.writeText(text);
    button.replaceChildren(replyIcon("check")); button.title = "Copied";
    button.setAttribute("aria-label", "Copied");
    const status = button.parentElement.querySelector(".reply-copy-status");
    if (status) status.textContent = "Copied";
    setTimeout(() => {
      if (!button.isConnected) return;
      button.replaceChildren(replyIcon("copy")); button.title = label; button.setAttribute("aria-label", label);
      if (status) status.textContent = "";
    }, 1600);
  } catch (error) { flash("Could not copy: " + error.message); }
}
function downloadReplyText(text, kind, extension) {
  const blob = new Blob([text], {type: extension === "md" ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const link = el("a", {href: url, download: "ux46-" + kind + "-" + new Date().toISOString().replace(/\D/g, "") + "-1." + extension});
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
function replyTextActions(text, kind, extension) {
  const label = kind === "block" ? "this block" : "entire response";
  const wrap = el("div", {class: "reply-actions", role: "group", "aria-label": kind === "block" ? "Text block actions" : "Response actions"});
  wrap.appendChild(replyAction("copy", "Copy " + label, (event) => copyReplyText(text, event.currentTarget, "Copy " + label)));
  wrap.appendChild(replyAction("download", "Download " + label + " (." + extension + ")", () => downloadReplyText(text, kind, extension)));
  wrap.appendChild(el("span", {class: "reply-copy-status", role: "status", "aria-live": "polite"}));
  return wrap;
}

function mdCodeBlock(body, info, closed) {
  const wrap = el("div", {class: "mdcode"});
  const label = (info || "").trim();
  const language = label.split(/\s+/)[0].toLowerCase();
  const extensions = {javascript: "js", js: "js", typescript: "ts", ts: "ts", python: "py", py: "py", bash: "sh", sh: "sh", json: "json", markdown: "md", md: "md", yaml: "yaml", yml: "yml", html: "html", css: "css", sql: "sql"};
  const header = el("div", {class: "mdcode-head"});
  header.appendChild(el("span", {text: [label, !closed ? "streaming" : ""].filter(Boolean).join(" · ")}));
  header.appendChild(replyTextActions(body, "block", extensions[language] || "txt"));
  wrap.appendChild(header);
  wrap.appendChild(el("pre", {class: "out"}, [el("code", {text: body})]));
  return wrap;
}

function mdTableNode(headCells, aligns, rows, depth) {
  const wrap = el("div", {class: "mdtable-wrap", tabindex: "0", role: "group",
    "aria-label": "table, scrolls sideways"});
  const table = el("table", {class: "mdtable"});
  const thead = el("thead");
  const hrow = el("tr");
  headCells.forEach((cell, index) => {
    const th = el("th", {scope: "col", class: aligns[index] || null});
    th.appendChild(mdInline(cell, depth + 1));
    hrow.appendChild(th);
  });
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (let index = 0; index < headCells.length; index += 1) {
      const td = el("td", {class: aligns[index] || null});
      td.appendChild(mdInline(row[index] === undefined ? "" : row[index], depth + 1));
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

/* Strip up to `width` leading spaces, keeping deeper indentation intact. */
function mdDedent(line, width) {
  let cut = 0;
  while (cut < width && line[cut] === " ") cut += 1;
  return line.slice(cut);
}

function mdList(lines, start, host, depth) {
  const first = mdBullet(lines[start]) || mdOrdered(lines[start]);
  const ordered = !mdBullet(lines[start]);
  const base = first[1].length;
  const items = [];
  let current = null;
  let loose = false;
  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    if (mdBlank(line)) {
      const next = lines[i + 1];
      const continues = next !== undefined && !mdBlank(next)
        && (mdBullet(next) || mdOrdered(next) || /^ {2,}\S/.test(next));
      if (!continues) break;
      loose = true;
      if (current) current.push("");
      i += 1;
      continue;
    }
    const item = mdBullet(line) || mdOrdered(line);
    if (item && item[1].length <= base + 1) {
      if (Boolean(mdBullet(line)) === ordered) break;   // a different list starts here
      current = [item[3]];
      items.push({lines: current, width: item[1].length + item[2].length + 2});
      i += 1;
      continue;
    }
    if (!current) break;
    if (item || /^ {2,}\S/.test(line) || !mdBreaksParagraph(line)) {
      current.push(mdDedent(line, items[items.length - 1].width));
      i += 1;
      continue;
    }
    break;
  }
  if (!items.length) return start;
  const list = el(ordered ? "ol" : "ul", {class: "mdlist" + (loose ? " loose" : "")});
  const startAt = ordered ? parseInt(first[2], 10) : 1;
  if (ordered && startAt !== 1 && startAt >= 0) list.setAttribute("start", String(startAt));
  for (const item of items) {
    const li = el("li");
    const inner = document.createDocumentFragment();
    mdBlocks(item.lines, inner, depth + 1);
    // A tight one-paragraph item reads better without the paragraph box.
    if (inner.childNodes.length === 1 && inner.firstChild.tagName === "P") {
      while (inner.firstChild.firstChild) li.appendChild(inner.firstChild.firstChild);
    } else {
      li.appendChild(inner);
    }
    list.appendChild(li);
  }
  host.appendChild(list);
  return i;
}

function mdBlocks(lines, host, depth) {
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (mdBlank(line)) { i += 1; continue; }

    const fence = mdFenceOpen(line);
    if (fence) {
      const marker = fence[1][0];
      const need = fence[1].length;
      const body = [];
      let j = i + 1;
      let closed = false;
      while (j < lines.length) {
        const close = mdFenceClose(lines[j]);
        if (close && close[1][0] === marker && close[1].length >= need) { closed = true; break; }
        body.push(lines[j]);
        j += 1;
      }
      host.appendChild(mdCodeBlock(body.join("\n"), fence[2], closed));
      i = closed ? j + 1 : j;
      continue;
    }

    const heading = mdHeading(line);
    if (heading) {
      const level = heading[1].length;
      const tag = "h" + Math.min(6, level + 2);
      const node = el(tag, {class: "mdh mdh" + level});
      node.appendChild(mdInline(heading[2].replace(/[ \t]+#+[ \t]*$/, ""), 0));
      host.appendChild(node);
      i += 1;
      continue;
    }

    if (mdRule(line)) { host.appendChild(el("hr")); i += 1; continue; }

    if (mdQuote(line)) {
      const inner = [];
      let j = i;
      while (j < lines.length
             && (mdQuote(lines[j]) || (inner.length && !mdBreaksParagraph(lines[j])))) {
        inner.push(lines[j].replace(/^ {0,3}> ?/, ""));
        j += 1;
      }
      const quote = el("blockquote", {class: "mdquote"});
      if (depth < MD_MAX_DEPTH) mdBlocks(inner, quote, depth + 1);
      else quote.appendChild(el("p", null, [mdInline(inner.join("\n"), depth)]));
      host.appendChild(quote);
      i = j;
      continue;
    }

    if ((mdBullet(line) || mdOrdered(line)) && depth < MD_MAX_DEPTH) {
      const next = mdList(lines, i, host, depth);
      if (next > i) { i = next; continue; }
    }

    if (line.includes("|") && lines[i + 1] !== undefined && mdDivider(lines[i + 1])) {
      const head = mdCells(line);
      const aligns = mdCells(lines[i + 1]).map(mdAlign);
      if (head.length > 1 && aligns.length === head.length) {
        const rows = [];
        let j = i + 2;
        while (j < lines.length && !mdBlank(lines[j]) && lines[j].includes("|")) {
          rows.push(mdCells(lines[j]));
          j += 1;
        }
        host.appendChild(mdTableNode(head, aligns, rows, depth));
        i = j;
        continue;
      }
    }

    const para = [line];
    let j = i + 1;
    while (j < lines.length && !mdBreaksParagraph(lines[j])) {
      if (lines[j].includes("|") && lines[j + 1] !== undefined && mdDivider(lines[j + 1])) break;
      para.push(lines[j]);
      j += 1;
    }
    host.appendChild(el("p", null, [mdInline(para.join("\n"), depth)]));
    i = j;
  }
}

/* Render agent Markdown into a fragment. Very long text stays plain, because
   an honest wall of exact text beats a slow guess. */
function markdownFragment(text) {
  const source = String(text || "").replace(/\r\n?/g, "\n");
  const frag = document.createDocumentFragment();
  if (!source.trim()) return frag;
  if (source.length > MD_MAX_CHARS) {
    frag.appendChild(el("pre", {class: "out", text: source}));
    return frag;
  }
  try {
    mdBlocks(source.split("\n"), frag, 0);
  } catch (error) {
    // Never lose the reply to a rendering bug: fall back to the exact text.
    console.warn("markdown render failed, showing exact text", error);
    frag.replaceChildren();
    frag.appendChild(el("pre", {class: "out", text: source}));
  }
  return frag;
}

function agentBody(item) {
  const body = el("div", {class: "body md"});
  body.appendChild(markdownFragment(item.text || ""));
  return body;
}

/* The exact reported text is available, but does not compete with the reply
   itself. It lives in the reply's quiet overflow control. */
function replyMenu(item) {
  const menu = el("details", {class: "reply-menu"});
  menu.appendChild(el("summary", {title: "Reply options", "aria-label": "Reply options", text: "⋯"}));
  menu.appendChild(el("button", {
    class: "reply-menu-item", type: "button", text: "Original text",
    on: {click: (event) => { menu.open = false; openSource(item, event.currentTarget); }},
  }));
  return menu;
}

/* ------------------------------------------------------------ the activity
   One line above the composer that says what this native session is doing,
   built only from what the runtime reported: the submission this browser is
   making, the active turn the room state reports, and the newest item of that
   turn. Nothing is invented, nothing cycles on a timer, and when no finer
   detail is reported "Working" is the honest thing to say. */

const DONE_STATUS = new Set(["completed", "complete", "failed", "error", "aborted",
                             "cancelled", "canceled", "timeout", "interrupted"]);

function stepDone(item) {
  if (item.exit_code !== undefined && item.exit_code !== null) return true;
  return DONE_STATUS.has(String(item.status || "").toLowerCase());
}

/* A concise label for one reported step. Provider text is passed through as
   given; nothing here paraphrases a summary or guesses an intention. */
function stepLabel(item) {
  if (!item) return null;
  if (item.type === "commandExecution") {
    return {verb: "Running", what: item.command || "a command", mono: true};
  }
  if (item.type === "fileChange") {
    const paths = (item.changes || []).map((c) => c.path).filter(Boolean);
    return {verb: paths.length === 1 ? "Editing" : "Editing " + (paths.length || "") + " files",
            what: paths.join(", "), mono: true};
  }
  if (item.type === "mcpToolCall" || item.type === "dynamicToolCall") {
    return {verb: "Calling", what: (item.server ? item.server + " · " : "") + (item.tool || "a tool"),
            mono: true};
  }
  if (item.type === "webSearch") return {verb: "Searching", what: item.query || "the web", mono: false};
  if (item.type === "functionCallOutput") {
    return {verb: "Reading tool output", what: item.name || "", mono: true};
  }
  if (item.type === "plan") return {verb: "Updating the plan", what: "", mono: false};
  if (item.type === "reasoning") {
    const lines = (item.summary || []).map((line) => String(line).trim()).filter(Boolean);
    // A provider summary is shown exactly as the provider wrote it.
    return lines.length ? {verb: "", what: lines[lines.length - 1], mono: false} : null;
  }
  if (item.type === "agentMessage") return {verb: "Writing a reply", what: "", mono: false};
  if (item.type === "userMessage") return null;
  return {verb: "Working", what: item.type, mono: true};
}

function activityItems() {
  return state.tail.length ? state.tail : state.items;
}

function turnOf(items, turnId) {
  return turnId ? items.filter((i) => i.turn_id === turnId) : [];
}

/* The one true reading of what is happening, in order of certainty. */
function activityState() {
  const detail = state.detail;
  if (!detail || !detail.controllable) return null;
  if (state.connKind === "off") {
    return {kind: "offline", live: false,
            text: "Connection lost — UX46 cannot see this session right now"};
  }
  if (state.roomRefreshing) return {kind:"offline",live:false,text:"Checking for newer activity…"};
  if (viewFreshnessError()) return {kind: "offline", live: false,
    text: "Last known view — " + viewFreshnessError()};
  if (state.sending) return {kind: "sending", live: true, text: "Sending your message"};

  const waiting = state.approvals.filter((a) => a.room === state.room);
  if (waiting.length) {
    return {kind: "needs", live: false,
            text: "Waiting for you · " + String(waiting[0].kind || "a decision").replace(/_/g, " ")};
  }

  const items = activityItems();
  const native = detail.native || {};
  const acceptedTurn = state.accepted && state.accepted.room === state.room ? state.accepted.turn : "";
  const failedTurn = detail.native_terminal?.turn_id;
  const active = (native.active_turn && native.active_turn !== failedTurn ? native.active_turn : "")
    || (acceptedTurn && acceptedTurn !== failedTurn ? acceptedTurn : "");
  if (!active && native.active_run === true) {
    // A runtime can report that a run is in flight without giving it an id.
    // That is worth saying, and no id is invented to say it.
    return {kind: "working", live: true, text: "Working"};
  }
  if (active) {
    const steps = turnOf(items, active);
    const newest = steps.length ? steps[steps.length - 1] : null;
    const label = newest && !(stepDone(newest) && newest.type !== "agentMessage")
      ? stepLabel(newest) : null;
    return {kind: "working", live: true, turn: active,
            steps: steps.filter((i) => WORK_TYPES.has(i.type)).length,
            text: label ? [label.verb, label.what].filter(Boolean).join(" ") : "Working",
            mono: Boolean(label && label.mono && label.what)};
  }

  const terminal = detail.native_terminal;
  if (["limited", "sign_in_required"].includes(accountState(detail))) {
    return {kind: "failed", live: false, text: accountSummary(detail)};
  }
  if (detail.connection_recovery?.state === "refreshed") {
    return {kind: "idle", live: false, text: connectionSummary(detail)};
  }
  if (terminal && terminalSummary(detail)) {
    return {kind: "failed", live: false, text: terminalSummary(detail)};
  }
  if (state.outcome && state.outcome.room === state.room) {
    return {kind: state.outcome.kind, live: false, text: state.outcome.text};
  }

  // UX46 reports no active turn. That is not proof a turn ended well: a
  // session held in someone's terminal has no turn here either, and an
  // interrupted or failed turn also leaves its last work item behind.
  const last = items[items.length - 1];
  if (!last) return null;
  const steps = turnOf(items, last.turn_id).filter((i) => WORK_TYPES.has(i.type)).length;
  if (last.type === "agentMessage") {
    // A reported final answer is the one ending the runtime states outright.
    return {kind: "answered", live: false, turn: last.turn_id, steps,
            text: last.phase === "final_answer" ? "Answered" : "Replied"};
  }
  if (WORK_TYPES.has(last.type)) {
    return {kind: "idle", live: false, turn: last.turn_id, steps,
            text: "Last reported work"};
  }
  return null;
}

function renderActivity() {
  const host = $("#activity");
  if (!host) return;
  const now = activityState();
  host.replaceChildren();
  if (!now) { host.hidden = true; return; }
  host.hidden = false;
  host.dataset.kind = now.kind;
  const line = el("div", {class: "act-line"});
  line.appendChild(el("span", {class: "act-dot" + (now.live ? " live" : ""), "aria-hidden": "true"}));
  line.appendChild(el("span", {class: "act-text" + (now.mono ? " mono" : ""), text: now.text}));
  if (now.kind === "failed") {
    line.appendChild(el("button", {class: "act-more", type: "button", text: "Refresh this agent",
      on: {click: () => { openWorkspaceRecovery("agent", agentId()); void runWorkspaceRecovery(); }}}));
    line.appendChild(el("button", {class: "act-more", type: "button", text: "Recovery",
      on: {click: () => { openRoomSettings(); $("#connDetails").scrollIntoView({block: "nearest"}); }}}));
  }
  if (state.freshness.historyError) {
    line.appendChild(el("button", {class: "act-more", type: "button", text: "Read latest",
      on: {click: async () => {
        const seq = state.roomSeq, roomId = state.room;
        await loadHistory();
        if (stale(seq, roomId)) return;
        if (!state.freshness.historyError) toTail();
        await refreshRoomState();
      }}}));
  }
  if (now.steps) {
    line.appendChild(el("span", {class: "act-count",
      text: now.steps === 1 ? "1 step" : now.steps + " steps"}));
    const open = isTurnOpen(now.turn);
    line.appendChild(el("button", {
      class: "act-more", type: "button", "aria-expanded": String(open),
      title: open ? "Collapse this turn's work" : "Show this turn's tool steps and output",
      text: open ? "Hide work" : "Show work",
      on: {click: () => toggleTurnWork(now.turn)},
    }));
  }
  host.appendChild(line);
  // An anchored history page may not contain the current turn. Show only its
  // reported work here without replacing history or moving the reader.
  if (now.turn && isTurnOpen(now.turn) && !turnGroupKeys(now.turn).length) {
    const steps = turnOf(activityItems(), now.turn).filter(item => WORK_TYPES.has(item.type) && !emptyReasoning(item));
    if (steps.length) host.appendChild(el("section", {class: "activity-work", "aria-label": "Current turn work"}, [workRun(steps)]));
  }
}

/* The activity line's control opens the same work run that is in the
   transcript, so there is one place the steps live. */
function turnGroupKeys(turnId) {
  const keys = [];
  let run = null;
  for (const item of state.items) {
    if (WORK_TYPES.has(item.type) && item.turn_id === turnId) {
      if (!emptyReasoning(item) && !run) { run = item.id; keys.push(run); }
    } else {
      run = null;
    }
  }
  return keys;
}

function turnWorkOpen(turnId) {
  return state.openTurns[turnId] === undefined ? !state.folded : state.openTurns[turnId];
}
function workItemOpen(item) {
  return state.openWork[item.id] === undefined ? turnWorkOpen(item.turn_id) : state.openWork[item.id];
}
function isTurnOpen(turnId) {
  const keys = turnGroupKeys(turnId);
  const loaded = turnOf(state.items, turnId).filter(item => WORK_TYPES.has(item.type) && !emptyReasoning(item));
  const steps = loaded.length ? loaded : turnOf(activityItems(), turnId).filter(item => WORK_TYPES.has(item.type) && !emptyReasoning(item));
  if (!loaded.length) return steps.length > 0 && state.openTurns[turnId] === true
    && groupOpen(steps[0].id, turnId) && steps.every(workItemOpen);
  return steps.length > 0 && keys.every(key => groupOpen(key, turnId)) && steps.every(workItemOpen);
}

function toggleTurnWork(turnId) {
  const open = !isTurnOpen(turnId);
  state.openTurns[turnId] = open;
  for (const key of turnGroupKeys(turnId)) delete state.openGroups[key];
  for (const item of [...state.items, ...state.tail]) {
    if (item.turn_id === turnId) { delete state.openWork[item.id]; delete state.openGroups[item.id]; }
  }
  redrawKeepingPlace();
  renderActivity();
}

function groupOpen(key, turnId) {
  return state.openGroups[key] === undefined ? turnWorkOpen(turnId) : state.openGroups[key];
}

/* Consecutive tool steps from the same turn read as one run the person can
   open or collapse. Any message — the answer, or a prompt typed mid-turn —
   ends the run, so nothing can ever be hidden behind one of these rows. */
function workRun(run) {
  const key = run[0].id;
  if (run.length === 1) return workNode(run[0], key);
  const open = groupOpen(key, run[0].turn_id);
  const last = stepLabel(run[run.length - 1]);
  const wrap = el("div", {class: "workrun", data: {run: key}});
  wrap.appendChild(el("button", {
    class: "work-toggle run", type: "button", "aria-expanded": String(open),
    on: {click: () => { state.openGroups[key] = !open; redrawKeepingPlace(); renderActivity(); }},
  }, [
    workGlyph(run[run.length - 1]),
    el("span", {class: "run-last", text: last ? [last.verb, last.what].filter(Boolean).join(" ") : ""}),
    el("span", {class: "ttl", text: "· " + run.length + " steps"}),
    useIcon("i-chev", "chev"),
  ]));
  if (open) {
    const body = el("div", {class: "run-body"});
    for (const step of run) body.appendChild(workNode(step));
    wrap.appendChild(body);
  }
  return wrap;
}

/* The person at this console. Their name is known, so the transcript uses it
   rather than a pronoun; no device or location is claimed for a message the
   runtime recorded without one. */
const HUMAN_NAME = "User";

function messageNode(item) {
  if (item.type === "userMessage") {
    const node = el("article", {class: "msg human", data: {mid: item.id}});
    const head = el("div", {class: "head", title: itemHead(item)}, [
      el("span", {class: "who", text: HUMAN_NAME}),
    ]);
    node.appendChild(head);
    const managed = managedAttachmentsFor(item);
    const text = managed.length
      ? withoutMatchedPlaceholders(item.text || "", managed.length)
      : (item.text || "");
    if (text.trim() || !managed.length) node.appendChild(el("div", {class: "body", text}));
    if (managed.length) node.appendChild(attachmentStrip(managed));
    return node;
  }
  if (item.type === "agentMessage") {
    // Finality is the phase the runtime reported. Nothing is inferred from text.
    const final = item.phase === "final_answer";
    const node = el("article", {class: "msg agent" + (final ? " final" : ""), data: {mid: item.id}});
    // Who answered, and where from: the selected agent's own label and the
    // project this session belongs to. Finality is the reported phase.
    const head = el("div", {class: "head", title: itemHead(item)});
    // Whose answer this is, said with the same face the picker uses. The
    // name is right beside it, so nothing here depends on recognising a
    // colour, and the mark is small enough to stay out of the way on a phone.
    const author = (state.agents || []).find((a) => a.id === agentId())
      || {id: agentId(), label: agentLabel()};
    head.appendChild(agentAvatar(author, {size: "tiny"}));
    head.appendChild(el("span", {class: "who", text: agentLabel()}));
    if (state.detail && state.detail.project_name) {
      head.appendChild(el("span", {class: "kindtag plain", text: "· " + state.detail.project_name}));
    }
    if (final) {
      head.appendChild(el("span", {class: "finaltag",
        title: "the native session reported this item as its final answer",
        text: "Final answer"}));
    } else {
      head.appendChild(el("span", {class: "kindtag plain",
        text: item.phase ? String(item.phase).replace("_", " ") : "phase not reported"}));
    }
    head.appendChild(replyMenu(item));
    node.appendChild(head);
    node.appendChild(agentBody(item));
    for (const question of item.questions || []) {
      node.appendChild(el("p", {class: "meta", text: "asked: " + question.title}));
    }
    if ((item.text || "").trim()) {
      const actions = replyTextActions(item.text, "response", "md");
      if (state.voice.enabled) actions.prepend(listenControls(item));
      node.appendChild(actions);
    }
    return node;
  }
  return workNode(item);
}

function renderStream() {
  const earlier = $("#btnEarlier");
  if (earlier) {
    earlier.disabled = !state.cursor;
    earlier.title = state.cursor
      ? "Load the previous page of native history"
      : "The runtime reported no earlier page for this session";
  }
  const stream = $("#thread");
  stream.replaceChildren();
  const detail = state.detail;
  if (!detail) {
    const failure = state.roomError;
    if (failure && !failure.gone) {
      stream.appendChild(el("div", {class: "notice stop"}, [
        el("div", {text: "Could not open " + failure.room + " just now."}),
        el("div", {text: failure.message}),
        el("div", {text: "It is still your selected room and your draft is untouched — "
          + "nothing was sent anywhere else."}),
        el("div", {class: "acts"}, [
          el("button", {class: "linkbtn", type: "button", text: "Try again",
            on: {click: async (event) => {
              event.currentTarget.disabled = true;
              await selectRoom(failure.room, {toTail: true, connect: true});
            }}}),
        ]),
      ]));
    } else if (failure && failure.gone) {
      stream.appendChild(el("div", {class: "notice stop"}, [
        el("div", {text: failure.room + " is no longer in the registry."}),
      ]));
    } else {
      stream.appendChild(el("p", {class: "empty",
        text: "Choose a room to read its native session."}));
    }
    return;
  }
  state.roomError = null;
  if (!detail.controllable) {
    const nativeAgent = (state.agents || []).find(a => a.id !== agentId()
      && a.runtime === detail.runtime && a.node === detail.node);
    stream.appendChild(el("div", {class: "notice"}, [
      el("div", {text: detail.capability_label}),
      el("div", {text: "Showing saved Session Vault context. This view is not connected to the native conversation."}),
      nativeAgent ? el("button", {class: "linkbtn", type: "button",
        text: "Open with " + nativeAgent.label,
        on: {click: () => openSession(nativeAgent.id, detail.id, {connect: true, toTail: true})}}) : null,
    ]));
  }
  if (detail.checkpoint) {
    const cp = detail.checkpoint;
    stream.appendChild(el("div", {class: "notice"}, [
      el("div", {text: "Reported checkpoint · " + (cp.state || "?") + " / need " + (cp.need || "?")}),
      el("div", {text: "by " + (cp.reporter || "unknown") + " at " + (cp.at || "unknown time")
        + " — reported, not verified now"}),
      cp.next ? el("div", {text: "next: " + cp.next}) : null,
    ]));
  }
  if (detail.native_terminal && terminalSummary(detail)) {
    const terminal = detail.native_terminal;
    const refreshed = detail.connection_recovery?.state === "refreshed";
    stream.appendChild(el("div", {class: "notice" + (["limited", "sign_in_required"].includes(accountState(detail)) ? " stop" : "")}, [
      el("div", {text: terminalSummary(detail)}),
      el("div", {text: accountState(detail) === "unknown"
        ? "This describes a previous attempt. Current account availability has not been confirmed."
        : (terminal.message || "The native runtime reported a terminal failure.")}),
      refreshed ? el("div", {text: connectionSummary(detail)}) : null,
      el("div", {text: "UX46 did not resend the accepted message."}),
    ]));
  }
  if (state.historyUnavailable) {
    stream.appendChild(el("div", {class: "notice stop"}, [
      el("div", {text: "This session's history could not be read, so nothing is shown. "
        + "That is not the same as an empty conversation."}),
      el("div", {text: state.historyUnavailable.message}),
      el("div", {class: "acts"}, [
        el("button", {class: "linkbtn", type: "button", text: "Try again",
          on: {click: async (event) => {
            event.currentTarget.disabled = true;
            await loadHistory();
          }}}),
      ]),
      el("div", {text: "Your draft and target are untouched."}),
    ]));
  } else if (state.historySource === "empty") {
    stream.appendChild(el("div", {class: "notice"}, [
      el("div", {text: "This native session has no saved history yet — a new conversation."}),
    ]));
  }
  if (!state.complete && state.items.length) {
    stream.appendChild(el("div", {class: "notice"}, [
      el("div", {text: "Earlier history exists in this native session and has not been loaded."}),
      el("div", {class: "acts"}, [
        el("button", {class: "linkbtn", type: "button", text: "Load earlier",
          on: {click: loadEarlier}}),
      ]),
    ]));
  }
  const ownership = detail.ownership || {};
  if (ownership.state === "held_elsewhere") {
    stream.appendChild(el("div", {class: "notice stop"}, [
      el("div", {text: "Open in another terminal — finish or exit there to continue here."}),
      el("div", {text: "Reading is safe; UX46 will not send into a session another process holds."}),
    ]));
  } else if (ownership.state === "unknown") {
    stream.appendChild(el("div", {class: "notice stop"}, [
      el("div", {text: "UX46 cannot verify who holds this native session, so sending is refused."}),
      el("div", {text: String(ownership.detail || "")}),
    ]));
  }
  let index = 0;
  while (index < state.items.length) {
    const item = state.items[index];
    if (!WORK_TYPES.has(item.type)) {
      stream.appendChild(messageNode(item));
      index += 1;
      continue;
    }
    const run = [];
    while (index < state.items.length) {
      const step = state.items[index];
      if (!WORK_TYPES.has(step.type) || step.turn_id !== item.turn_id) break;
      if (!emptyReasoning(step)) run.push(step);
      index += 1;
    }
    if (run.length) stream.appendChild(workRun(run));
  }
  if (!state.items.length && detail.controllable) {
    stream.appendChild(el("p", {class: "empty", text: "No native history was returned for this session."}));
  }
  // A message UX46 queued sits where it was written, after the history and
  // before anything the runtime is asking for.
  for (const queued of pendingShown()) stream.appendChild(pendingNode(queued));
  for (const approval of state.approvals.filter((a) => a.room === state.room)) {
    stream.appendChild(approvalNode(approval));
  }
  applySelection();
  renderActivity();
}

function approvalNode(approval) {
  const params = approval.params || {};
  const lines = [];
  if (approval.kind.startsWith("command")) {
    lines.push("command: " + (params.command || ""));
    if (params.cwd) lines.push("in " + params.cwd);
  } else if (approval.kind.startsWith("file_change")) {
    lines.push("files: " + ((params.paths || []).join(", ") || "not reported"));
  } else if (approval.kind === "permissions") {
    lines.push("permission request in " + (params.cwd || ""));
  }
  if (params.reason) lines.push("reason: " + params.reason);

  const node = el("div", {class: "notice", data: {approval: approval.key}});
  node.appendChild(el("div", {text: "The native runtime is asking you before it continues."}));
  for (const line of lines) node.appendChild(el("div", {text: line}));

  if (approval.kind === "user_input") {
    for (const question of params.questions || []) {
      node.appendChild(el("div", {text: question.title}));
      const acts = el("div", {class: "acts"});
      for (const option of question.options.length ? question.options : ["ok"]) {
        acts.appendChild(el("button", {
          class: "linkbtn", type: "button", text: option,
          on: {click: () => answerApproval(approval, "answer", {[question.id]: option})},
        }));
      }
      node.appendChild(acts);
    }
    return node;
  }
  node.appendChild(el("div", {class: "acts"}, [
    el("button", {class: "primary", type: "button", text: "Accept once",
      on: {click: () => answerApproval(approval, "accept")}}),
    el("button", {class: "danger", type: "button", text: "Decline",
      on: {click: () => answerApproval(approval, "decline")}}),
  ]));
  node.appendChild(el("div", {text: "Accepting answers this one request. It does not change the "
    + "session's standing permissions."}));
  return node;
}

async function answerApproval(approval, decision, answers) {
  try {
    await api("/api/approvals/answer", {method: "POST", body: {
      key: approval.key, kind: approval.kind, decision, answers: answers || null,
    }});
    state.approvals = state.approvals.filter((a) => a.key !== approval.key);
    renderStream();
    renderBoard();
    updateNeedsCount();
  } catch (error) {
    setSendState("Could not answer that request: " + error.message, "fail");
  }
}

/* Speech is requested only by Listen. Keep the media element outside the
   transcript: routine redraws replace that DOM and must not restart playback.
   The small controls node is reused; leaving the room disposes both. */
function clearSpeech() {
  for (const entry of state.audio.values()) {
    if (entry.audio) {
      entry.audio.pause();
      entry.audio.removeAttribute("src");
      entry.audio.load();
      entry.audio.remove();
    }
  }
  state.audio.clear();
}

function speechTime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return Math.floor(value / 60) + ":" + String(value % 60).padStart(2, "0");
}

function listenControls(item) {
  let entry = state.audio.get(item.id);
  if (entry) return entry.node;
  entry = {node: el("div", {class: "listen", data: {listen: item.id}}), phase: "idle"};
  entry.node.appendChild(replyAction("listen", "Listen to response · local voice", () => speakItem(item.id, entry)));
  return entry.node;
}

function speechFailure(itemId, entry, message) {
  entry.phase = "error";
  entry.node.removeAttribute("aria-busy");
  entry.node.replaceChildren(
    el("span", {class: "listen-status", role: "status", text: message}),
    el("button", {class: "linkbtn", type: "button", text: "Try again", on: {click: () => speakItem(itemId, entry)}}));
}

function audioPlayer(entry, speech, owner) {
  const audio = el("audio", {preload: "auto", hidden: true,
    src: agentPath(owner, speech.audio_url)});
  entry.audio = audio;
  document.body.appendChild(audio);
  const box = el("div", {class: "speech-player", role: "group", "aria-label": "Response audio"});
  const status = el("span", {class: "listen-status", role: "status", text: "Loading audio…"});
  const play = el("button", {class: "speech-play", type: "button", disabled: true,
    "aria-label": "Play response", title: "Play response"}, [replyIcon("play")]);
  const back = el("button", {class: "speech-skip", type: "button", text: "−10",
    "aria-label": "Back 10 seconds", title: "Back 10 seconds", disabled: true});
  const forward = el("button", {class: "speech-skip", type: "button", text: "+10",
    "aria-label": "Forward 10 seconds", title: "Forward 10 seconds", disabled: true});
  const seek = el("input", {class: "speech-seek", type: "range", min: "0", max: "0", step: "0.1",
    value: "0", disabled: true, "aria-label": "Audio position"});
  const time = el("span", {class: "speech-time", text: "0:00 / —"});
  const speed = el("select", {class: "speech-speed", "aria-label": "Playback speed"},
    [0.75, 1, 1.25, 1.5, 2].map(rate => el("option", {value: rate, text: rate + "×", selected: rate === 1})));
  const duration = () => Number.isFinite(audio.duration) ? audio.duration : 0;
  const sync = () => {
    const total = duration();
    seek.max = String(total); seek.disabled = !total;
    seek.value = String(audio.currentTime || 0);
    seek.setAttribute("aria-valuetext", speechTime(audio.currentTime) + " of " + speechTime(total));
    time.textContent = speechTime(audio.currentTime) + " / " + (total ? speechTime(total) : "—");
    back.disabled = forward.disabled = !total;
    play.disabled = audio.readyState < 2;
    const label = audio.paused ? (audio.ended ? "Replay response" : "Play response") : "Pause response";
    play.setAttribute("aria-label", label); play.title = label;
    play.replaceChildren(replyIcon(audio.paused ? "play" : "pause"));
  };
  const start = async () => {
    for (const other of state.audio.values()) if (other !== entry && other.audio) other.audio.pause();
    try { await audio.play(); }
    catch (_) { status.textContent = "Ready · press Play to listen"; sync(); }
  };
  play.addEventListener("click", () => audio.paused ? void start() : audio.pause());
  const jump = value => { if (duration()) audio.currentTime = Math.max(0, Math.min(duration(), value)); sync(); };
  back.addEventListener("click", () => jump(audio.currentTime - 10));
  forward.addEventListener("click", () => jump(audio.currentTime + 10));
  seek.addEventListener("input", () => jump(Number(seek.value)));
  speed.addEventListener("change", () => { audio.playbackRate = Number(speed.value); });
  for (const event of ["loadedmetadata", "durationchange", "timeupdate", "seeked", "play", "pause", "ended"])
    audio.addEventListener(event, sync);
  audio.addEventListener("playing", () => { status.textContent = "Playing"; });
  audio.addEventListener("pause", () => { status.textContent = audio.ended ? "Finished" : "Paused"; });
  audio.addEventListener("ended", () => { status.textContent = "Finished"; });
  audio.addEventListener("waiting", () => { status.textContent = "Buffering…"; });
  const retry = el("button", {class: "linkbtn", type: "button", text: "Reload audio", hidden: true,
    on: {click: () => { retry.hidden = true; status.textContent = "Loading audio…"; audio.load(); }}});
  audio.addEventListener("error", () => { status.textContent = "Audio couldn’t load."; play.disabled = true; retry.hidden = false; });
  // Listen expresses playback intent; browsers that disallow delayed playback
  // still expose an enabled Play button rather than silently doing nothing.
  audio.addEventListener("canplay", () => { sync(); status.textContent = "Ready"; void start(); }, {once: true});
  audio.addEventListener("canplay", sync);
  box.append(back, play, forward, seek, time, speed, status, retry);
  if (speech.complete === false) box.appendChild(el("span", {class: "speech-partial", text:
    "Partial reading · " + speech.spoken_chars + " of " + speech.total_chars + " characters"}));
  entry.node.replaceChildren(box);
  entry.node.removeAttribute("aria-busy");
  entry.phase = "ready";
}

async function speakItem(itemId, entry) {
  if (!state.detail || entry.phase === "preparing") return;
  state.audio.set(itemId, entry);
  const roomId = state.detail.id, owner = agentId(), gen = state.agentGen;
  const mine = () => state.room === roomId && gen === state.agentGen && state.audio.get(itemId) === entry;
  entry.phase = "preparing";
  entry.node.setAttribute("aria-busy", "true");
  entry.node.replaceChildren(el("span", {class: "speech-preparing", role: "status"}, [
    el("span", {class: "speech-pulse", "aria-hidden": "true"}, [el("i"), el("i"), el("i")]),
    el("span", {text: "Preparing audio…"})]));
  try {
    const speech = await api(agentPath(owner, "/api/room/" + encodeURI(roomId) + "/speak"), {
      absolute: true, method: "POST", body: {item_id: itemId, voice: state.voice.preferred || undefined},
    });
    if (!mine()) return;
    if (typeof speech.audio_url !== "string" || !/^\/api\/audio\/[a-zA-Z0-9_-]+\.wav$/.test(speech.audio_url))
      throw new Error("The voice service returned an invalid audio location.");
    audioPlayer(entry, speech, owner);
  } catch (error) {
    if (mine()) speechFailure(itemId, entry, "Couldn’t prepare audio. " + error.message);
  }
}

function renderVoicePicker() {
  const pick = $("#voicePick");
  const note = $("#voiceNote");
  const select = $("#voiceSelect");
  if (!pick) return;
  if (!state.voice.enabled) {
    pick.hidden = false;
    select.hidden = true;
    note.textContent = "Local reading voice is off: "
      + (state.voice.reason || "not configured on this server") + ".";
    return;
  }
  pick.hidden = false;
  select.hidden = false;
  select.replaceChildren();
  for (const voice of state.voice.voices) {
    select.appendChild(el("option", {value: voice, text: voice,
      selected: voice === state.voice.preferred}));
  }
  note.textContent = "Runs locally on this machine. Nothing is spoken automatically.";
}

/* ------------------------------------------------------------ scroll rules */
function distanceFromBottom() {
  const stream = $("#stream");
  return stream.scrollHeight - stream.scrollTop - stream.clientHeight;
}
function toTail() { const s = $("#stream"); s.scrollTop = s.scrollHeight; }
// Following is reading intent, not a measurement taken before fonts, images,
// or the composer finish laying out. Only a person's scroll pauses it.
let tailFrame = 0;
function followLayout() {
  if (tailFrame) return;
  tailFrame = requestAnimationFrame(() => {
    tailFrame = 0;
    if (state.following && !state.sel && !state.anchor) toTail();
  });
}

function redrawKeepingPlace() {
  const stream = $("#stream");
  const keep = stream.scrollTop;
  renderStream();
  stream.scrollTop = keep;
}

function updateNewPill() {
  const pill = $("#newPill");
  if (state.newCount > 0) {
    $("#newLbl").textContent = state.newCount === 1 ? "1 new item below" : state.newCount + " new items below";
    pill.hidden = false;
  } else {
    pill.hidden = true;
  }
  syncFloaters();
}

/* The jump and new-item controls sit in the flow above the composer, so they
   take no space when there is nothing to say and never cover Send. */
function syncFloaters() {
  const host = $("#floaters");
  if (!host) return;
  host.hidden = $("#newPill").hidden && $("#selNav").hidden;
}

async function backToLive() {
  state.sel = null;
  const hadAnchor = state.anchor;
  state.anchor = null;
  state.newCount = 0;
  updateNewPill();
  if (hadAnchor) await loadHistory();   // return to the live tail page
  else renderStream();
  state.following = true;
  toTail();
}

/* ------------------------------------------------------------- room loading */
/* Loading the catalogue fills in what rooms are; it never decides which rooms
   the person is working in. Tabs come from what they opened, so a background
   search or a history page cannot put a room on the tab strip. */
async function loadRooms(query, options) {
  const opts = options || {};
  const params = new URLSearchParams();
  if (query) params.set("query", query);
  if (opts.project) params.set("project", opts.project);
  if (opts.ids && opts.ids.length) params.set("ids", opts.ids.join(","));
  params.set("limit", String(opts.limit || 60));
  if (opts.offset) params.set("offset", String(opts.offset));
  if (opts.group) params.set("group", "1");
  params.set("workers", opts.workers === "only" ? "only" : (opts.workers === "1" ? "1" : "0"));
  params.set("complete", opts.complete === false ? "0" : "1");
  const payload = await browseApi("/api/rooms?" + params.toString());
  for (const room of payload.rooms) state.rooms.set(room.id, room);
  return payload;
}

const roomViews = new Map();
let roomSelectionIntent = 0;
const VIEW_FIELDS = ["items", "tail", "cursor", "complete", "sel", "anchor", "searchHits", "newCount", "openWork", "openGroups", "openTurns", "accepted", "outcome", "conflict", "historySource", "historyNote", "historyUnavailable", "following"];
function rememberRoomView() {
  if (!state.room || !state.detail) return;
  const view = {detail:state.detail, draft:{...state.draft}, draftDirty:state.draftDirty, text:$("#draft").value};
  for (const key of VIEW_FIELDS) view[key] = state[key];
  roomViews.delete(state.room); roomViews.set(state.room,view);
  while (roomViews.size > 16) roomViews.delete(roomViews.keys().next().value);
}
/* Open a conversation. `opts.connect` says this was somebody choosing it, in
   which case it connects on arrival when the session is free; without it the
   room is read and drawn and nothing is touched. */
async function selectRoom(roomId, opts) {
  closeCommands();
  if (!roomId) return false;
  const agent = agentId();
  const gen = state.agentGen;
  const intent = ++roomSelectionIntent;
  stopDictation();
  const previous = state.room;
  if (previous && previous !== roomId) {
    state.positions[previous] = $("#stream").scrollTop;
    await flushDraft();
    if (intent !== roomSelectionIntent) return false;
    rememberRoomView();
  }
  if (previous !== roomId) clearSpeech();
  const seq = ++state.roomSeq;
  const cached = roomViews.get(roomId);
  state.room = roomId;
  state.freshness = {room: 0, history: 0, roomError: "", historyError: ""};
  state.roomRefreshing = true;
  state.roomError = null; state.roomGone = false;
  state.items = []; state.ids = new Set(); state.cursor = null;
  state.pendingEdit = null; clearTimeout(pendingTimer);
  if (state.attach && !(state.attach.agent === agentId() && state.attach.room === roomId)) {
    state.attach = null;
  }
  state.complete = true; state.sel = null; state.anchor = null;
  state.searchHits = []; state.newCount = 0; state.openWork = {}; state.openGroups = {}; state.openTurns = {};
  state.tail = []; state.accepted = null; state.outcome = null; state.conflict = null;
  state.historyUnavailable = null;
  clearReceipt();
  dismissOverlay(); showView("console");
  if (cached) {
    state.detail = cached.detail;
    state.draft = {...cached.draft}; state.draftDirty = cached.draftDirty;
    $("#draft").value = cached.text;
    for (const key of VIEW_FIELDS) state[key] = cached[key];
    state.ids = new Set(state.items.map(i=>i.id));
    renderTabs(); renderCrumb(); renderTarget(); renderStream(); renderActivity();
    $("#stream").scrollTop = state.positions[roomId] || 0;
    if (opts && opts.toTail) {toTail();state.following=true;}
  } else {
    state.detail = null; state.draft = {body:"",version:0}; state.draftDirty = false;
    $("#draft").value = "";
    renderTabs(); renderCrumb(); renderTarget(); renderStream();
  }
  setSendState(cached ? "Checking for updates…" : "Opening…","ok");
  const read = ++state.roomRead;
  try {
    const detail = await api("/api/room/" + encodeURI(roomId));
    if (stale(seq,roomId) || gen !== state.agentGen || read !== state.roomRead) return false;
    state.freshness.room = Date.now();
    state.freshness.roomError = "";
    state.detail = detail;
    state.rooms.set(detail.id,Object.assign(state.rooms.get(detail.id) || {},detail));
    // A background refresh must never replace text typed into the warm view.
    if (!state.draftDirty && !state.conflict
        && Number(detail.draft.version || 0) >= Number(state.draft.version || 0)) {
      state.draft = {body:detail.draft.body || "",version:detail.draft.version || 0};
      seedDraftWriter(detail.id,state.draft.version);
      $("#draft").value = state.draft.body;
    }
    state.roomRefreshing = false;
    state.approvals = (detail.approvals || []).map(a=>Object.assign({room:detail.id},a));
    renderTabs(); renderCrumb(); renderTarget();
    if (detail.controllable) {
      if (cached) await refreshTail(); else await loadHistory(seq,roomId);
    } else {state.items=[];renderStream();}
    if (stale(seq,roomId)) return false;
    if (!cached) {
      const position=state.positions[roomId];
      if (position !== undefined && !(opts && opts.toTail)) {
        $("#stream").scrollTop=position;state.following=distanceFromBottom()<FOLLOW_PX;
      } else {toTail();state.following=true;}
    }
    await loadPending(roomId, seq);
    if (stale(seq, roomId)) return false;
    if (state.pending.some((p) => p.room === roomId)) { redrawKeepingPlace(); schedulePendingPoll(); }
    reportDraftState(); rememberRoom(roomId);
    openTab(agentId(), roomId, detail);
    renderTabs(); refreshPanelForRoom();
    if (opts && opts.connect) await maybeConnect(agent, roomId, seq, gen);
    return true;
  } catch(error) {
    if (stale(seq,roomId) || gen !== state.agentGen || read !== state.roomRead) return false;
    state.roomRefreshing=false;
    state.freshness.roomError = "Conversation could not be checked";
    state.roomGone=error.status===404;
    if (cached && !state.roomGone) {
      state.detail={...state.detail,ownership:{state:"unknown",detected:false}};
      renderTarget();
      setSendState("Showing the saved view — connection could not be checked. Your draft is kept.","warn");
      renderActivity(); renderTabs();
    } else {
      state.detail=null;
      state.roomError={message:error.message,gone:state.roomGone,room:roomId};
      renderCrumb();renderTarget();renderStream();
      setSendState(state.roomGone ? "that room is no longer in the registry" : "could not open " + roomId + ": " + error.message,"fail");
    }
    return false;
  }
}

/* Keep the saved room and the shareable link in step, without a router. */
function rememberRoom(roomId) {
  if (roomId) openTab(agentId(), roomId, state.detail);
  publishActive();
  try {
    if (roomId) window.localStorage.setItem(agentKey("atlas.room"), roomId);
    else window.localStorage.removeItem(agentKey("atlas.room"));
  } catch (e) { /* private mode: the deep link still works */ }
  try {
    const url = new URL(window.location.href);
    if (roomId) url.searchParams.set("room", roomId);
    else url.searchParams.delete("room");
    window.history.replaceState(null, "", url.toString());
  } catch (e) { /* nothing depends on the URL */ }
}

async function loadHistory(seq, roomId) {
  const detail = state.detail;
  if (!detail || !detail.controllable) return;
  seq = seq === undefined ? state.roomSeq : seq;
  roomId = roomId || detail.id;
  const gen = state.agentGen, read = ++state.historyRead;
  try {
    const payload = await api("/api/room/" + encodeURI(roomId) + "/history?limit=40&direction=desc");
    if (stale(seq, roomId) || gen !== state.agentGen || read !== state.historyRead) return;
    if (payload.unavailable) {
      state.historyUnavailable = {message: payload.message || "History unavailable", code: payload.error_code || ""};
      throw new Error(state.historyUnavailable.message);
    }
    const ascending = payload.items.slice().reverse();
    state.items = ascending;
    state.tail = ascending;
    state.ids = new Set(ascending.map((i) => i.id));
    state.cursor = payload.next_cursor || null;
    state.complete = !!payload.complete;
    state.historySource = payload.source || "";
    state.historyNote = payload.note || "";
    state.historyUnavailable = null;
    state.freshness.history = Date.now();
    state.freshness.historyError = "";
    renderStream();
    renderActivity();
  } catch (error) {
    if (stale(seq, roomId) || gen !== state.agentGen || read !== state.historyRead) return;
    state.freshness.historyError = "History could not be checked";
    renderActivity();
    renderStream();
    return false;
  }
}

async function loadEarlier() {
  const detail = state.detail;
  if (!detail || !state.cursor) return;
  const roomId = detail.id, seq = state.roomSeq, gen = state.agentGen;
  const cursor = state.cursor, read = ++state.earlierRead, historyRead = state.historyRead;
  const stream = $("#stream");
  state.following = false;
  const before = stream.scrollHeight;
  const payload = await api("/api/room/" + encodeURI(roomId)
    + "/history?limit=40&direction=desc&cursor=" + encodeURIComponent(cursor));
  // Same room names on two agents are different conversations. A newer
  // history read also invalidates this page's cursor and scroll adjustment.
  if (stale(seq, roomId) || gen !== state.agentGen || read !== state.earlierRead
      || historyRead !== state.historyRead || cursor !== state.cursor) return;
  if (payload.unavailable) throw new Error(payload.message || "History unavailable");
  const older = payload.items.slice().reverse().filter((i) => !state.ids.has(i.id));
  for (const item of older) state.ids.add(item.id);
  state.items = older.concat(state.items);
  state.cursor = payload.next_cursor || null;
  state.complete = !!payload.complete;
  renderStream();
  stream.scrollTop = stream.scrollHeight - before + stream.scrollTop;
}

/* A native item keeps its id while it changes: a command gains output, a
   status and an exit code, and an agent message grows as it streams. Matching
   on id and replacing in place is what makes the console show the run rather
   than only its first frame. */
function mergeTail(page) {
  const at = new Map(state.items.map((item, index) => [item.id, index]));
  let added = 0;
  let changed = 0;
  for (const item of page) {
    const index = at.get(item.id);
    if (index === undefined) {
      state.items.push(item);
      state.ids.add(item.id);
      added += 1;
    } else if (JSON.stringify(state.items[index]) !== JSON.stringify(item)) {
      state.items[index] = item;          // same id, same place, newer content
      changed += 1;
    }
  }
  return {added, changed};
}

async function refreshTail() {
  const detail = state.detail;
  if (!detail || !detail.controllable) return true;
  const seq = state.roomSeq, roomId = detail.id, gen = state.agentGen;
  const read = ++state.historyRead;
  const obsolete = () => stale(seq, roomId) || gen !== state.agentGen || read !== state.historyRead;
  const stream = $("#stream");
  // Catch up to the previously observed tail, not merely the newest 25 items.
  // Otherwise a suspended browser could silently splice over a missing interval.
  const previous = (state.tail.length ? state.tail : state.items).at(-1)?.id;
  let rows = [], cursor = null, payload;
  try {
    for (let page = 0; page < 20; page++) {
      payload = await api("/api/room/" + encodeURI(roomId) + "/history?limit=40&direction=desc"
        + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
      if (obsolete()) return false;
      if (payload.unavailable) throw new Error(payload.message || "History unavailable");
      rows.push(...payload.items);
      if (!previous || rows.some(item => item.id === previous) || payload.complete) break;
      if (!payload.next_cursor || cursor === payload.next_cursor || page === 19) {
        throw new Error("History catch-up exceeded its bounded window");
      }
      cursor = payload.next_cursor;
    }
    const keep = stream.scrollTop;
    const following = state.following && !state.sel && !state.anchor;
    state.tail = rows.slice(0, 40).reverse();
    state.freshness.history = Date.now();
    state.freshness.historyError = "";
    settleAccepted();
    if (state.anchor) { renderActivity(); return true; }
    const empty = !state.items.length;
    const {added, changed} = mergeTail(rows.reverse());
    if (empty) {
      state.cursor = payload.next_cursor || null;
      state.complete = !!payload.complete;
    }
    if (!added && !changed) { renderActivity(); return true; }
    renderStream();
    renderActivity();
    if (following) { toTail(); followLayout(); }
    else {
      stream.scrollTop = keep;
      if (added) { state.newCount += added; updateNewPill(); }
    }
    return true;
  } catch (error) {
    if (obsolete()) return false;
    state.freshness.historyError = "History could not be checked";
    renderActivity();
    return false;
  }
}

/* An accepted submission carries the console until the room state or the
   history says what really happened; it never claims work indefinitely. */
const ACCEPTED_GRACE_MS = 90000;
function settleAccepted() {
  const pending = state.accepted;
  if (!pending) return;
  if (pending.room !== state.room || Date.now() - pending.at > ACCEPTED_GRACE_MS) {
    state.accepted = null;
    return;
  }
  const finished = state.tail.some((item) => item.turn_id === pending.turn
    && item.type === "agentMessage" && item.phase === "final_answer");
  if (finished) state.accepted = null;
}

/* ------------------------------------------------------------- the composer */
/* The destination and its connection state belong to the session heading;
   the composer keeps only the field and what it can do with it. */
function renderTarget() {
  const detail = state.detail;
  const own = $("#ownState");
  const send = $("#btnSend");
  const draft = $("#draft");
  const cont = $("#btnContinue");
  const plus = $("#btnPlus");
  renderModelControl();
  if (!detail) {
    own.textContent = "";
    own.className = "ownership";
    send.disabled = true; draft.disabled = true; cont.hidden = true;
    if (plus) plus.disabled = true;
    return;
  }
  const ownership = detail.ownership || {};
  const label = {
    atlas_owned: ["connected", "ok"],
    connecting: ["connecting…", ""],
    idle: ["not connected", "warn"],
    held_elsewhere: ["open in another terminal", "warn"],
    unknown: ["cannot check who holds it", "warn"],
    not_controllable: [detail.capability_short || "no control", ""],
  }[ownership.state] || [ownership.state || "", ""];
  own.textContent = state.roomRefreshing ? "checking connection…" : label[0];
  own.className = "ownership " + (label[1] || "");
  const canSend = detail.controllable && ownership.state === "atlas_owned";
  // Idle is what selecting the session connects to on its own. Unknown is
  // offered rather than assumed: this console will not decide a session is
  // free when the host could not tell it so, but the person may still ask.
  cont.hidden = !(detail.controllable
                  && (ownership.state === "idle" || ownership.state === "unknown"));
  // An attempt that was left behind by a room or agent switch must not leave
  // its button reading "Connecting…" over somebody else's session.
  setAttachButton(attachingHere());
  draft.disabled = !detail.controllable;
  if (plus) plus.disabled = !canSend;
  send.disabled = state.roomRefreshing || (!canSend && !draft.value.trim().startsWith("/")) || state.sending;
  $("#draftLabel").textContent = "Draft for " + detail.id;
  draft.setAttribute("aria-label", "Draft for " + (detail.title || detail.session)
    + " in " + detail.project_name);
  renderDetails();
  renderConflict();
  renderAttachNote();
}

function reportDraftState() {
  if (state.receipt && Date.now() < state.receipt.until) {
    // A Send click while a file is uploading is deliberately not queued for
    // later dispatch. Once the last upload settles, that temporary refusal is
    // no longer true; keeping it for the generic receipt lifetime makes a
    // ready attachment contradict the composer for up to twenty seconds.
    const waitingForUploads = state.receipt.text === "Wait for the uploads to finish.";
    const stillUploading = currentUploads().some((entry) => entry.status === "chosen"
      || entry.status === "uploading");
    if (!waitingForUploads || stillUploading) {
      setSendState(state.receipt.text, state.receipt.kind);
      return;
    }
  }
  state.receipt = null;
  const detail = state.detail;
  if (!detail) { setSendState("", "ok"); return; }
  if (state.conflict && state.conflict.room === detail.id) {
    setSendState("draft conflict — choose which text to keep", "fail");
    return;
  }
  // Every other routine state now has a permanent home that is not the
  // composer: the destination and its connection state are in the session
  // heading, the transcript already explains a session held elsewhere, and
  // the saved draft version is in Connection details. The field stays quiet
  // until something has actually happened to report.
  const uploads = currentUploads().filter(
    (entry) => entry.status === "chosen" || entry.status === "uploading" || entry.status === "failed");
  if (uploads.length) {
    const failed = uploads.filter((entry) => entry.status === "failed");
    setSendState(failed.length
      ? (failed.length === 1 ? failed[0].name + " did not upload — retry or remove it"
         : failed.length + " files did not upload — retry or remove them")
      : (uploads.length === 1 ? "uploading " + uploads[0].name + "…"
         : "uploading " + uploads.length + " files…"),
      failed.length ? "fail" : "ok");
    return;
  }
  setSendState("", "ok");
}

let draftTimer = null;

/* ---------------------------------------------------------- draft autosave
   One writer per room. Every save is a compare-and-swap against the version
   the server last acknowledged to this browser, and only one request per room
   is ever in flight: anything typed meanwhile coalesces into a single
   follow-up that starts from the acknowledged version.

   Without that serialization two debounces — or a debounce and the flush a
   room switch does — could leave with the same base version. The second one
   then fails compare-and-swap against the first one's own write, and this
   browser accused itself of being "another device". The versions differed by
   exactly the character typed in between. Genuine concurrent writers are
   still caught, because the base is only ever a version we were told about. */
const draftWrites = new Map();      // room id -> {version, next, running, waiters}

function draftWriter(roomId, version) {
  let entry = draftWrites.get(roomId);
  if (!entry) {
    entry = {version: version || 0, next: null, running: null, waiters: []};
    draftWrites.set(roomId, entry);
  }
  return entry;
}

/* A freshly read room tells us its version. Only trust that while this room's
   writer is idle: a save already in flight is about to know better. */
function seedDraftWriter(roomId, version) {
  const entry = draftWriter(roomId, version);
  if (!entry.running && !entry.next) entry.version = Number(version) || 0;
}

/* Every save names the room and base version it was typed against, so a slow
   response can never land on a room the person has since left. */
async function saveDraftFor(roomId, body, baseVersion) {
  return api("/api/room/" + encodeURI(roomId) + "/draft", {
    method: "PUT", body: {body, base_version: baseVersion, device: state.device},
  });
}

/* Queue this exact text for its room. Newer text replaces anything still
   queued, so a burst of keystrokes costs one more request, not one each. */
function queueDraftSave(roomId, seq, text) {
  if (state.conflict && state.conflict.room === roomId) return Promise.resolve();
  const entry = draftWriter(roomId, state.room === roomId ? state.draft.version : 0);
  entry.next = {text, seq};
  if (!entry.running) entry.running = pumpDraft(roomId, entry);
  return draftSettled(roomId);
}

/* Resolves when this room has nothing left to write. */
function draftSettled(roomId) {
  const entry = draftWrites.get(roomId);
  if (!entry || !entry.running) return Promise.resolve();
  return new Promise((resolve) => entry.waiters.push(resolve));
}

async function pumpDraft(roomId, entry) {
  try {
    while (entry.next) {
      if (state.conflict && state.conflict.room === roomId) { entry.next = null; break; }
      const {text, seq} = entry.next;
      entry.next = null;
      await attemptDraftSave(roomId, seq, text, entry);
    }
  } finally {
    entry.running = null;
    for (const resolve of entry.waiters.splice(0)) resolve();
  }
}

async function attemptDraftSave(roomId, seq, text, entry) {
  const mine = () => state.room === roomId && seq === state.roomSeq;
  try {
    const saved = await saveDraftFor(roomId, text, entry.version);
    entry.version = Number(saved.version) || 0;
    if (mine()) {
      state.draft = {body: saved.body, version: saved.version};
      // Whatever is on screen now is the truth: if it moved on while this
      // request was in flight, the draft is still unsaved and says so.
      state.draftDirty = $("#draft").value !== saved.body;
      reportDraftState();
    }
    return true;
  } catch (error) {
    if (error.code === "draft_conflict" && error.detail) {
      // The base was a version this browser had been given, so something else
      // really did write in between. Both texts are kept; nothing is saved
      // again for this room until the person chooses.
      entry.version = Number(error.detail.version) || entry.version;
      entry.next = null;
      if (mine()) {
        state.conflict = {room: roomId, mine: text, theirs: error.detail};
        state.draftDirty = true;
        renderConflict();
        setSendState("this draft also changed somewhere else — choose which text to keep", "fail");
      }
      return false;
    }
    if (mine()) {
      state.draftDirty = true;
      setSendState("draft not saved: " + error.message + " — your text is still here", "fail");
    }
    return false;
  }
}

async function flushDraft() {
  clearTimeout(draftTimer);
  if (!state.detail) return;
  const roomId = state.detail.id;
  if (state.conflict && state.conflict.room === roomId) return;  // never guess
  if (state.draftDirty) queueDraftSave(roomId, state.roomSeq, $("#draft").value);
  await draftSettled(roomId);
}

function renderConflict() {
  const host = $("#conflict");
  host.replaceChildren();
  const conflict = state.conflict;
  if (!conflict || conflict.room !== state.room) { host.hidden = true; return; }
  host.hidden = false;
  // "Yours" is whatever is in the box right now, including anything typed
  // since the refusal — that is what "Keep mine" will save.
  const mine = $("#draft").value;
  host.appendChild(el("div", {text: "This room's draft also changed somewhere else — "
    + "another tab, another browser, or another device."}));
  host.appendChild(el("div", {class: "two"}, [
    el("div", {class: "half"}, [
      el("p", {class: "lbl", text: "Yours, still in the box"}),
      el("p", {class: "txt", text: mine.slice(0, 300) || "(empty)"}),
    ]),
    el("div", {class: "half"}, [
      el("p", {class: "lbl", text: "The other writer, v" + conflict.theirs.version}),
      el("p", {class: "txt", text: (conflict.theirs.body || "").slice(0, 300) || "(empty)"}),
    ]),
  ]));
  host.appendChild(el("div", {class: "acts"}, [
    el("button", {class: "linkbtn", type: "button", text: "Keep mine",
      on: {click: () => resolveConflict("mine")}}),
    el("button", {class: "linkbtn", type: "button", text: "Use the other one",
      on: {click: () => resolveConflict("theirs")}}),
  ]));
}

async function resolveConflict(choice) {
  const conflict = state.conflict;
  if (!conflict) return;
  const roomId = conflict.room;
  const sameRoom = state.room === roomId;
  const theirVersion = Number(conflict.theirs.version) || 0;
  // Keep mine means the text on screen now, never the snapshot from when the
  // save was refused, so typing during the choice is not thrown away.
  const keep = choice === "mine"
    ? (sameRoom ? $("#draft").value : conflict.mine)
    : (conflict.theirs.body || "");
  state.conflict = null;
  const entry = draftWriter(roomId, theirVersion);
  entry.version = theirVersion;
  renderConflict();

  if (choice === "theirs") {
    // Their text is already the saved text at that version. Adopt it instead
    // of writing it back, so no one else sees a pointless new version.
    if (sameRoom) {
      $("#draft").value = keep;
      state.draft = {body: keep, version: theirVersion};
      state.draftDirty = false;
      setSendState("using the other text · v" + theirVersion, "saved");
    }
    return;
  }
  if (sameRoom) state.draftDirty = true;
  await queueDraftSave(roomId, sameRoom ? state.roomSeq : -1, keep);
  if (state.room === roomId && !state.conflict && !state.draftDirty) {
    setSendState("kept your text · v" + state.draft.version, "saved");
  }
}

function scheduleDraftSave() {
  clearReceipt();
  state.draftDirty = true;
  reportDraftState();
  // While a conflict is open the box is still live; keep "yours" showing it.
  if (state.conflict && state.conflict.room === state.room) renderConflict();
  clearTimeout(draftTimer);
  draftTimer = setTimeout(flushDraft, 500);
}

async function send() {
  const detail = state.detail;
  const textarea = $("#draft");
  const body = textarea.value.trim();
  if (state.sending) return;
  if (state.roomRefreshing) {setReceipt("Checking this session’s connection before sending…","warn");return;}
  if (!detail) {
    setSendState("no room is open — choose one before sending", "fail");
    renderTarget();
    return;
  }
  if (!body) { setSendState("there is nothing to send", "ok"); return; }
  if (body.startsWith("/")) { await sendCommand(body, true); return; }
  const ownership = (detail.ownership || {}).state;
  if (ownership !== "atlas_owned") {
    setSendState(ownership === "idle" || !ownership
      ? "not connected to this session — choose Continue here first"
      : "cannot send while this session is " + ownership.replace("_", " "), "warn");
    renderTarget();
    return;
  }

  // Pin everything this send belongs to before the first await — including
  // which agent it goes through, because that is the only agent that will ever
  // be able to answer for it.
  const sendAgent = agentId();
  const roomId = detail.id;
  const seq = state.roomSeq;
  const threadId = (detail.native || {}).thread_id || "";
  const snapshot = textarea.value;
  const sentText = body;
  const id = clientId();
  const durable = rememberAttempt({
    agent: sendAgent, client_id: id, room: roomId, thread_id: threadId,
    text: sentText, at: Date.now(),
  });

  // Our own save below is authoritative for this room; a queued debounce
  // would race it with a stale base version.
  clearTimeout(draftTimer);
  state.sending = true;
  state.outcome = null;
  renderTarget();
  setSendState("sending", "ok");
  renderActivity();          // say "Sending" before the request leaves

  try {
    const result = await api("/api/room/" + encodeURI(roomId) + "/submit", {
      method: "POST", body: {client_id: id, body: sentText, thread_id: threadId},
    });
    const submission = result.submission || {};
    const sameRoom = state.room === roomId && seq === state.roomSeq;
    if (submission.status === "accepted") {
      forgetAttempt(sendAgent, id);
      // The runtime took the turn; carry that until the room state confirms it.
      state.accepted = {room: roomId, turn: submission.native_turn_id || "", at: Date.now()};
      await settleDraftAfterSend(roomId, seq, sentText, snapshot, sameRoom);
      const receipt = agentLabel(sendAgent) + " received your message";
      if (sameRoom) { setReceipt(receipt, "saved"); await refreshTail(); } else flash(receipt);
    } else if (submission.status === "uncertain") {
      state.outcome = {room: roomId, kind: "failed",
                       text: "The runtime did not answer — nothing was resent"};
      const text = "uncertain — nothing was resent; your text is kept";
      if (sameRoom) setReceipt(text, "fail"); else flash(text);
      showRecovery(sendAgent, id, roomId, sentText, "the runtime did not answer in time");
    } else {
      forgetAttempt(sendAgent, id);
      state.outcome = {room: roomId, kind: "failed",
                       text: "Not sent · " + (submission.detail || "the runtime refused it")};
      const text = "not sent: " + (submission.detail || "the runtime refused it");
      if (sameRoom) setReceipt(text, "fail"); else flash(text);
    }
  } catch (error) {
    const proven = PRE_DISPATCH_REFUSALS.has(error.code);
    const sameRoom = state.room === roomId && seq === state.roomSeq;
    if (proven) {
      // The console told us it refused this before dispatching.
      forgetAttempt(sendAgent, id);
      state.outcome = {room: roomId, kind: "failed", text: "Not sent · " + error.message};
      const text = "not sent: " + error.message + " — your text is still here";
      if (sameRoom) setReceipt(text, "fail"); else flash(text);
    } else {
      // A connection can fail after the server accepted the turn, so this is
      // unknown, not refused. The attempt id is kept for an exact lookup.
      state.outcome = {room: roomId, kind: "failed",
                       text: "Uncertain — the connection failed after sending"};
      const text = durable
        ? "uncertain — the connection failed after sending; UX46 will check this "
          + "exact message instead of sending it again"
        : "uncertain — the connection failed and this browser cannot store the "
          + "attempt, so it may not be recoverable after a reload";
      if (sameRoom) setReceipt(text, "fail"); else flash(text);
      const saved = readOutboxFor(sendAgent).find(entry => entry.client_id === id);
      if (saved) rememberAttempt({...saved, failure: {
        message: error.message || "Connection failed", code: error.code || "",
        status: error.status || 0, at: Date.now()}});
      showRecovery(sendAgent, id, roomId, sentText, error.message);
    }
  } finally {
    state.sending = false;
    if (state.room === roomId && seq === state.roomSeq) { await refreshRoomState(); await refreshTail(); }
    else renderTarget();
    renderActivity();
  }
}

/* After an accepted send, the draft is whatever is left — including anything
   typed while the send was in flight. It goes through the same per-room
   writer, so it cannot race an autosave that is still in the air. */
async function settleDraftAfterSend(roomId, seq, sentText, snapshot, sameRoom) {
  const textarea = $("#draft");
  const base = sameRoom ? textarea.value : snapshot;
  const remainder = base.startsWith(sentText)
    ? base.slice(sentText.length).replace(/^\s+/, "") : base;
  if (sameRoom) { textarea.value = remainder; resizeDraft(); }
  if (state.room === roomId && seq === state.roomSeq) state.draftDirty = true;
  await queueDraftSave(roomId, seq, remainder);
  if (state.room === roomId && seq === state.roomSeq && state.draftDirty
      && !(state.conflict && state.conflict.room === roomId)) {
    // Either they typed again or the write failed; both want another attempt.
    clearTimeout(draftTimer);
    draftTimer = setTimeout(flushDraft, 500);
  }
}

function shortName(roomId) {
  if (state.detail && state.detail.id === roomId) {
    return state.detail.title || state.detail.session || roomId;
  }
  const tab = findTab(agentId(), roomId);
  if (tab && tab.title) return tab.title;
  const room = state.rooms.get(roomId);
  return room ? (room.title || room.session) : roomId;
}

/* ------------------------------------------------------- message recovery
   A send whose outcome this browser never learned. The rules are narrow and
   they matter, because the thing being reported is whether words the person
   wrote reached anyone.

   An attempt belongs to the agent it was sent through. That agent is recorded
   the moment the attempt is stored, every later lookup is addressed to it by
   name, and no answer from any other agent is allowed to speak for it — asking
   the currently selected agent about an id another agent minted returns "no
   such submission", which then reads as "nothing was sent" about a message
   that was in fact delivered.

   A missing receipt is a missing receipt. It is never reported as proof that
   nothing was sent.

   Every card can be dismissed, in every state. Dismissing hides it and records
   that choice across reloads; it does not throw the text away, so a message can
   still be recovered afterwards. Nothing here ever resends anything. */

/* One outbox per agent, so two agents' attempts can never be read as each
   other's — and so the buckets an older build wrote are still found. */
function outboxKeyFor(agent) {
  return (agent || DEFAULT_AGENT) === DEFAULT_AGENT ? OUTBOX_KEY : OUTBOX_KEY + "@" + agent;
}

function readOutboxFor(agent) {
  try {
    const raw = window.localStorage.getItem(outboxKeyFor(agent));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) { return []; }
}

function writeOutboxFor(agent, entries) {
  try {
    window.localStorage.setItem(outboxKeyFor(agent), JSON.stringify(entries.slice(-20)));
    return true;
  } catch (e) { return false; }
}

/* Every unresolved attempt this browser holds, whichever agent made it. An
   entry written before attempts carried their agent takes it from the bucket
   it was found in, which is the same thing. */
function readAllAttempts() {
  const found = [];
  for (const agent of configuredIds()) {
    for (const entry of readOutboxFor(agent)) {
      if (!entry || !entry.client_id) continue;
      found.push(Object.assign({}, entry, {agent: entry.agent || agent}));
    }
  }
  return found.sort((a, b) => (a.at || 0) - (b.at || 0));
}

/* Remember an attempt before it leaves, so a lost response can be resolved by
   asking that exact agent about this exact id instead of sending again. */
function rememberAttempt(attempt) {
  const agent = attempt.agent || agentId();
  const entries = readOutboxFor(agent).filter((e) => e.client_id !== attempt.client_id);
  entries.push(Object.assign({}, attempt, {agent}));
  return writeOutboxFor(agent, entries);
}

function forgetAttempt(agent, clientId) {
  const id = agent || DEFAULT_AGENT;
  writeOutboxFor(id, readOutboxFor(id).filter((e) => e.client_id !== clientId));
}

/* Put away, not thrown away: the text stays recoverable and the card stays
   gone across reloads. */
function dismissAttempt(agent, clientId) {
  const id = agent || DEFAULT_AGENT;
  const entries = readOutboxFor(id).map((entry) =>
    entry.client_id === clientId ? Object.assign({}, entry, {dismissed: true}) : entry);
  writeOutboxFor(id, entries);
}

/* ------------------------------------------------------------- the cards */
function recoveryKey(agent, clientId) { return (agent || DEFAULT_AGENT) + " " + clientId; }

/* What to call the place this message was going. A readable name if anything
   knows one — the open room, its tab, this agent's catalogue — and only the
   room's own session segment as a last resort. Never a native id. */
function recoveryWhere(agent, room) {
  const bits = [agentLabel(agent)];
  let name = "";
  let project = "";
  if (agent === agentId() && state.detail && state.detail.id === room) {
    name = state.detail.title || state.detail.session;
    project = state.detail.project_name || "";
  }
  const tab = findTab(agent, room);
  if (!name && tab) { name = tab.title; project = project || tab.project; }
  if (!name && agent === agentId()) {
    const known = state.rooms.get(room);
    if (known) { name = known.title || known.session; project = project || known.project_name; }
  }
  if (project) bits.push(project);
  // Last resort is the room's own address, whole. Half of it ("ui") names
  // nothing a person can recognise.
  bits.push(name || room);
  return bits.filter(Boolean).join(" · ");
}

/* The words themselves, compactly. The local copy first; a receipt body from
   the agent covers the case where this browser no longer holds one. */
const RECOVERY_PREVIEW_CHARS = 160;
function recoveryPreview(record) {
  const raw = String(record.text || record.body || "").replace(/\s+/g, " ").trim();
  if (!raw) return "";
  return raw.length > RECOVERY_PREVIEW_CHARS
    ? raw.slice(0, RECOVERY_PREVIEW_CHARS - 1) + "…" : raw;
}

function findRecovery(key) { return state.recoveries.find((r) => r.key === key) || null; }

function noteRecovery(record) {
  const key = recoveryKey(record.agent, record.clientId);
  const held = findRecovery(key);
  if (held) Object.assign(held, record, {key});
  else state.recoveries.push(Object.assign({key}, record));
  renderRecoveries();
  return findRecovery(key);
}

function dropRecovery(key) {
  state.recoveries = state.recoveries.filter((r) => r.key !== key);
  renderRecoveries();
}

/* Put the words back where they were written, and nowhere else. A draft that
   already has something in it is never overwritten. */
async function restoreRecovery(record, button) {
  const text = record.text || record.body || "";
  if (!text) return;
  if (agentId() !== record.agent || state.room !== record.room) {
    if (button) { button.disabled = true; button.textContent = "Opening…"; }
    const ok = await openSession(record.agent, record.room, {toTail: true, connect: true});
    // Wherever we ended up, only that exact conversation may receive this.
    if (!ok || agentId() !== record.agent || state.room !== record.room) {
      if (button) { button.disabled = false; button.textContent = "Open that session"; }
      record.message = "Could not open that session, so the text was left where it is.";
      renderRecoveries();
      return;
    }
  }
  const textarea = $("#draft");
  if (textarea.value.trim()) {
    record.note = "The draft in that session is not empty, so nothing was overwritten. "
      + "The text is still kept here.";
    renderRecoveries();
    return;
  }
  textarea.value = text;
  resizeDraft();
  scheduleDraftSave();
  forgetAttempt(record.agent, record.clientId);
  dropRecovery(record.key);
  textarea.focus();
}

function recoveryCard(record) {
  const card = el("div", {class: "rec-card" + (record.tone ? " " + record.tone : ""),
                          data: {agent: record.agent, room: record.room, state: record.status},
                          title: record.turn ? "native turn " + record.turn : null});
  card.appendChild(el("p", {class: "rec-where", text: recoveryWhere(record.agent, record.room)}));
  card.appendChild(el("p", {class: "rec-what", text: record.message}));
  const preview = recoveryPreview(record);
  if (preview) card.appendChild(el("p", {class: "rec-quote", text: preview}));
  else {
    card.appendChild(el("p", {class: "rec-quote none",
      text: "This browser no longer holds the text of that message, and no receipt "
        + "carried it."}));
  }
  const originalFailure = readOutboxFor(record.agent).find(entry => entry.client_id === record.clientId)?.failure;
  if (originalFailure?.message) card.appendChild(el("p", {class: "rec-note",
    text: "Original error: " + originalFailure.message}));
  if (record.note) card.appendChild(el("p", {class: "rec-note", text: record.note}));

  const acts = el("div", {class: "acts"});
  if (record.canCheck) {
    acts.appendChild(el("button", {
      class: "linkbtn", type: "button", disabled: record.checking,
      text: record.checking ? "Checking…"
        : (record.checked ? "Check again" : "Check what happened"),
      on: {click: (event) => checkAttempt(record, event.currentTarget)},
    }));
  }
  if (record.text || record.body) {
    const away = agentId() !== record.agent || state.room !== record.room;
    acts.appendChild(el("button", {
      class: "linkbtn", type: "button",
      text: away ? "Open that session" : "Put the text back",
      title: away ? "Open " + recoveryWhere(record.agent, record.room)
        + " and put this text in its draft" : "Put this text back in the draft here",
      on: {click: (event) => restoreRecovery(record, event.currentTarget)},
    }));
  }
  // Always, in every state. A message this browser cannot explain must still
  // be something the person can put away.
  acts.appendChild(el("button", {
    class: "ghost rec-dismiss", type: "button", text: "Dismiss",
    title: record.settled
      ? "Put this away" : "Put this away — the text stays recoverable",
    on: {click: () => {
      if (!record.settled) dismissAttempt(record.agent, record.clientId);
      dropRecovery(record.key);
    }},
  }));
  card.appendChild(acts);
  return card;
}

function renderRecoveries() {
  const host = $("#recovery");
  if (!host) return;
  host.replaceChildren();
  if (!state.recoveries.length) { host.hidden = true; return; }
  host.hidden = false;
  for (const record of state.recoveries) host.appendChild(recoveryCard(record));
}

/* One unresolved attempt, resolvable by its exact id on its own agent. */
function showRecovery(agent, clientId, roomId, text, why) {
  return noteRecovery({
    agent: agent || DEFAULT_AGENT, clientId, room: roomId, text: text || "", body: "",
    status: "unknown", tone: "warn", settled: false, checked: false, checking: false,
    canCheck: configuredIds().has(agent || DEFAULT_AGENT),
    message: "The outcome of this message is unknown"
      + (why ? " (" + why + ")" : "") + ". Nothing was resent.",
    note: "", turn: "",
  });
}

const RECOVERY_WORDS = {
  accepted: "received your message",
  pending: "was journaled there but the runtime has not accepted it yet",
  uncertain: "has an unknown delivery; nothing will be resent",
  failed: "was refused; nothing was delivered",
};

async function checkAttempt(record, button) {
  if (record.checking) return;
  const agent = record.agent;
  const label = agentLabel(agent);
  if (!configuredIds().has(agent)) {
    // An agent this console no longer serves. Nothing here is going to guess
    // which of the remaining ones might have taken it.
    record.canCheck = false;
    record.message = "This message was sent through an agent this console no longer "
      + "serves, so its receipt cannot be looked up here.";
    renderRecoveries();
    return;
  }
  record.checking = true;
  renderRecoveries();
  try {
    // Addressed to the agent that made the attempt, never to the one selected
    // now: another agent has never heard of this id and would answer "no".
    const payload = await api(
      agentPath(agent, "/api/submissions/" + encodeURIComponent(record.clientId)),
      {absolute: true});
    const submission = payload.submission || {};
    const status = String(submission.status || "");
    record.checking = false;
    record.checked = true;
    record.status = status || "unknown";
    record.body = submission.body || record.body;
    record.turn = submission.native_turn_id || "";
    record.message = "On " + label + ", this message "
      + (RECOVERY_WORDS[status] || ("is recorded as " + (status || "an unreported state")))
      + ".";
    record.tone = status === "accepted" ? "" : (status === "failed" ? "stop" : "warn");
    // Settled either way: there is nothing left to recover, so the stored
    // attempt goes and the card is only waiting to be read.
    record.settled = status === "accepted" || status === "failed";
    if (record.settled) forgetAttempt(agent, record.clientId);
    record.canCheck = !record.settled;
  } catch (error) {
    record.checking = false;
    record.checked = true;
    record.canCheck = true;
    record.tone = "warn";
    if (error.status === 404) {
      // A 404 is the absence of a receipt, and nothing more. It does not prove
      // the message was not delivered — the route may not exist on this build,
      // and an adapter that keeps no journal answers exactly the same way.
      record.status = "noreceipt";
      record.message = error.code === "unknown_submission"
        ? "No receipt for this message on " + label + ". That agent has no record of "
          + "it, so UX46 cannot confirm whether it was delivered."
        : label + " did not answer a receipt lookup for this message, so UX46 cannot "
          + "confirm whether it was delivered.";
    } else {
      record.status = "unreachable";
      record.message = "Could not ask " + label + " about this message: "
        + (error.message || "the lookup failed") + ". UX46 cannot confirm whether it "
        + "was delivered.";
    }
  }
  renderRecoveries();
}

/* On load, surface anything left unresolved by a previous connection — from
   every agent, not only the one that happens to be selected. */
async function recoverAttempts() {
  const entries = readAllAttempts().filter((entry) => !entry.dismissed);
  if (!entries.length) return;
  const storageWorks = writeOutboxFor(DEFAULT_AGENT, readOutboxFor(DEFAULT_AGENT));
  const recent = entries.slice(-3);
  for (const entry of recent) {
    showRecovery(entry.agent, entry.client_id, entry.room, entry.text,
                 entry.failure?.message || "left unresolved before this page loaded");
  }
  if (!storageWorks) {
    for (const entry of recent) {
      const held = findRecovery(recoveryKey(entry.agent, entry.client_id));
      if (held) held.note = "This browser cannot store attempts, so recovery is not "
        + "durable here.";
    }
    renderRecoveries();
  }
  // Each is asked of its own agent, and each writes only into its own card.
  for (const entry of recent) {
    const held = findRecovery(recoveryKey(entry.agent, entry.client_id));
    if (held) await checkAttempt(held);
  }
}

/* An outcome for a room the person has already left still has to be seen. */
function flash(text) {
  const host = $("#flash");
  host.textContent = uiNotice(text);
  host.hidden = false;
  clearTimeout(flash._timer);
  flash._timer = setTimeout(() => { host.hidden = true; }, 12000);
}

async function refreshRoomState() {
  if (!state.room || state.roomRefreshing) return false;
  const seq = state.roomSeq, roomId = state.room, gen = state.agentGen;
  const read = ++state.roomRead;
  const obsolete = () => stale(seq, roomId) || gen !== state.agentGen || read !== state.roomRead;
  try {
    const detail = await api("/api/room/" + encodeURI(roomId));
    if (obsolete()) return false;
    state.detail = detail;
    state.freshness.room = Date.now();
    state.freshness.roomError = "";
    state.approvals = (detail.approvals || []).map((a) => Object.assign({room: detail.id}, a));
    if ((detail.native || {}).active_turn) state.accepted = null;
    settleAccepted();
    renderTarget(); renderCrumb(); reportDraftState(); renderActivity();
    renderGoalResumeControl();
    if (state.ui.dock === "attention") renderAttentionPanel();
    return true;
  } catch (error) {
    if (obsolete()) return false;
    state.freshness.roomError = "Conversation could not be checked";
    renderActivity(); renderTabs();
    if (state.ui.dock === "attention") renderAttentionPanel();
    return false;
  }
}

/* Why taking this session did not work, kept on screen until it is acted on.
   A routine state refresh clears the composer's one line, and this is exactly
   the message that must survive that. */
function renderAttachNote() {
  const host = $("#attachNote");
  if (!host) return;
  const note = state.attach;
  host.replaceChildren();
  if (!note || note.agent !== agentId() || note.room !== state.room) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  host.appendChild(el("div", {text: "Could not connect to " + note.name + "."}));
  host.appendChild(el("div", {text: note.message}));
  host.appendChild(el("div", {text: "Nothing was sent, and your draft is untouched."}));
  host.appendChild(el("div", {class: "acts"}, [
    el("button", {class: "linkbtn", type: "button", text: "Try again",
      on: {click: () => continueHere($("#btnContinue"))}}),
    el("button", {class: "ghost", type: "button", text: "Dismiss",
      on: {click: () => { state.attach = null; renderAttachNote(); }}}),
  ]));
}

function setAttachButton(busy) {
  const button = $("#btnContinue");
  if (!button) return;
  button.disabled = busy;
  button.textContent = busy ? "Connecting…" : "Continue here";
}

/* ------------------------------------------------------------- connecting
   Choosing a session connects to it. That used to be a button somebody had to
   find and press, which meant every conversation opened one step short of
   being usable; now selecting one is the ask, and the button is what is left
   for the times an attempt did not work.

   An attempt belongs to one agent and one room. They are kept by that pair
   rather than by a single flag, so connecting to one conversation can never
   swallow the connect for another, a second ask for the same one joins the
   attempt already running instead of racing it, and an answer that arrives
   after the person has moved on is dropped rather than drawn over whatever is
   on screen by then. */
const attaching = new Map();

function attachKey(agent, room) {
  return (agent || DEFAULT_AGENT) + "\u0000" + String(room || "");
}
function attachingHere() { return attaching.has(attachKey(agentId(), state.room)); }

/* Take this exact session, on this exact agent. Everything the answer will be
   compared against is pinned before the request leaves. Success is not "the
   call returned" — it is the room reporting ownership this console can
   actually send through. */
async function attachSession(agent, roomId, detail, auto) {
  const seq = state.roomSeq;
  const gen = state.agentGen;
  const name = detail.title || detail.session || roomId;
  const mine = () => agentId() === agent && gen === state.agentGen && !stale(seq, roomId);

  if (mine()) {
    state.attach = null;
    renderAttachNote();
    setSendState("connecting to " + name + "…", "ok");
  }
  try {
    // The body is empty. Connecting is connecting: it never carries a message,
    // a command or a prompt of any kind.
    const result = await api(agentPath(agent, "/api/room/" + encodeURI(roomId) + "/continue"),
                             {method: "POST", body: {}, absolute: true});
    const room = result.room || null;
    const owned = room && (room.ownership || {}).state === "atlas_owned";
    if (!mine()) return Boolean(owned);
    if (room) state.detail = room;
    // Re-read rather than trust: the room state the call returned is the same
    // one everything else reads, and the crumb and details must agree with it.
    renderTarget();
    renderCrumb();
    renderStream();
    if (!owned) {
      const own = (room && (room.ownership || {}).state) || "unknown";
      state.attach = {agent, room: roomId, name,
        message: "This console connected, but " + name + " still reports "
          + own.replace(/_/g, " ") + ", so it will not send into it."};
      renderAttachNote();
      setSendState("not connected — " + own.replace(/_/g, " "), "fail");
      return false;
    }
    // A connection nobody asked for out loud says so quietly: the heading
    // reads connected, and neither the caret nor a phone keyboard moves.
    if (auto) reportDraftState();
    else { setReceipt("connected to " + name + " — ready to send", "saved"); $("#draft").focus(); }
    return true;
  } catch (error) {
    if (!mine()) return false;
    state.attach = {agent, room: roomId, name,
                    message: error.message || "the runtime refused the connection"};
    renderAttachNote();
    setSendState("could not connect — see the reason above", "fail");
    await refreshRoomState();
    return false;
  }
}

async function continueHere(trigger, opts) {
  const detail = state.detail;
  if (!detail) return false;
  const agent = agentId();
  const roomId = detail.id;
  const key = attachKey(agent, roomId);
  if (!opts?.auto) manuallyDisconnected.delete(key);
  // The caller's own place in the world, so a result can be judged against
  // where *it* was rather than where the attempt it joined started.
  const seq = opts && opts.seq !== undefined ? opts.seq : state.roomSeq;
  const gen = opts && opts.gen !== undefined ? opts.gen : state.agentGen;
  const current = () => agentId() === agent && gen === state.agentGen && !stale(seq, roomId);
  // Already going. Wait for that one rather than asking twice — and then read
  // the room again on this caller's own behalf. Leaving a session and coming
  // back while its connect is still in flight is exactly how the attempt that
  // started it ends up being the wrong one to draw with: it answers for a room
  // nobody is in any more and drops its own result, correctly, leaving the
  // person looking at a connected session that still says otherwise.
  const held = attaching.get(key);
  if (held) {
    if (current()) setAttachButton(true);
    let ok = false;
    try { ok = await held; }
    finally { if (current()) setAttachButton(false); }
    if (current()) await refreshRoomState();
    return ok;
  }
  const attempt = attachSession(agent, roomId, detail, Boolean(opts && opts.auto));
  attaching.set(key, attempt);
  // There is one Continue button and it always belongs to whatever room is on
  // screen now, so it only ever reports on that room's own attempt.
  if (attachKey(agentId(), state.room) === key) setAttachButton(true);
  try { return await attempt; }
  finally {
    attaching.delete(key);
    if (attachKey(agentId(), state.room) === key) setAttachButton(false);
  }
}

/* What selecting a session does about connecting to it. It is deliberately
   narrow: only a session this console can drive, only one this host reports
   as free, and only once — a refusal is reported and left alone rather than
   asked again on every redraw. */
async function maybeConnect(agent, roomId, seq, gen) {
  if (agentId() !== agent || gen !== state.agentGen || stale(seq, roomId)) return false;
  const detail = state.detail;
  if (!detail || detail.id !== roomId || !detail.controllable) return false;
  if (manuallyDisconnected.has(attachKey(agent, roomId))) return false;
  const own = (detail.ownership || {}).state;
  // Already warm. Selecting it again must not touch the runtime at all.
  if (own === "atlas_owned") return true;
  if (own === "held_elsewhere") {
    // Somebody else is writing there. UX46 says so and stops; it has never
    // taken a session away from another process and does not start here.
    setSendState("open in another terminal — UX46 will not take it over", "warn");
    return false;
  }
  if (own !== "idle") {
    // Ownership this host could not establish is not ownership this console
    // may assume. The reason is on screen, and Continue here is the way to
    // ask anyway, deliberately, with the host still the one that decides.
    return false;
  }
  // Every arrival here is somebody choosing this session: a tab, a row, the
  // mark, a link. Nothing polls into it, so a refusal earlier is no reason to
  // refuse to ask now — picking a session again after it failed is a person
  // saying "try that again", and it would be strange to ignore them.
  return continueHere(null, {auto: true, seq, gen});
}

// A recovery action owns one fixed agent/room even if the user changes tabs.
// Reconnect never proceeds after an unknown release or repeats a message.
const sessionConnectionActions = new Set();
const manuallyDisconnected = new Set();
// Defaults belong to the selected agent's host. Never let a late response
// from another tab change the policy the person is looking at or saving.
let executionPolicyView = null;
let executionPolicyLoad = 0;
function renderExecutionPolicyActual() {
  if (executionPolicyView?.owner !== agentId()) $("#executionPolicyPanel").hidden = true;
  const profile = state.detail?.native?.execution_profile;
  const sandbox = profile?.sandbox?.type;
  $("#executionPolicyActual").textContent = "This session: " + (!profile?.granted
    ? "access has not been verified on this connection."
    : sandbox === "dangerFullAccess" ? "full OS access · approvals " + profile.approval_policy
    : sandbox === "workspaceWrite" ? "project access · approvals " + profile.approval_policy
    : "" + sandbox + " · approvals " + profile.approval_policy);
}
function renderExecutionPolicyChoice() {
  const choice = executionPolicyView?.choices.find(item => item.id === $("#executionPolicy").value);
  $("#executionPolicyHelp").textContent = choice?.description || "";
}
async function loadExecutionPolicy() {
  const owner = agentId(), ticket = ++executionPolicyLoad;
  executionPolicyView = null;
  $("#executionPolicyPanel").hidden = true;
  if (state.detail?.runtime !== "codex") return;
  try {
    const result = await api(agentPath(owner, "/api/execution-policy"), {absolute: true});
    if (ticket !== executionPolicyLoad || owner !== agentId()) return;
    executionPolicyView = {...result, owner};
    $("#executionPolicyLabel").textContent = agentLabel() + " · access default";
    $("#executionPolicy").replaceChildren(...result.choices.map(choice => el("option", {value: choice.id, text: choice.label})));
    $("#executionPolicy").value = result.policy;
    $("#saveExecutionPolicy").disabled = false;
    $("#executionPolicyStatus").textContent = "";
    $("#executionPolicyPanel").hidden = false;
    renderExecutionPolicyChoice(); renderExecutionPolicyActual();
  } catch (_) { /* Older adapters keep their existing connection controls. */ }
}
$("#executionPolicy").addEventListener("change", renderExecutionPolicyChoice);
$("#saveExecutionPolicy").addEventListener("click", async () => {
  const view = executionPolicyView;
  if (!view || view.owner !== agentId()) return;
  const selected = $("#executionPolicy").value;
  $("#saveExecutionPolicy").disabled = true;
  try {
    const result = await api(agentPath(view.owner, "/api/execution-policy"), {absolute: true,
      method: "POST", body: {policy: selected, base_revision: view.revision}});
    if (executionPolicyView !== view || view.owner !== agentId()) return;
    executionPolicyView = {...result, owner: view.owner};
    $("#executionPolicyStatus").textContent = "Default saved. Existing connections have not changed.";
  } catch (error) {
    if (executionPolicyView === view && view.owner === agentId())
      $("#executionPolicyStatus").textContent = error.message;
  } finally {
    if (view.owner === agentId()) $("#saveExecutionPolicy").disabled = false;
  }
});
async function changeSessionConnection(owner, room, reconnect) {
  const key = attachKey(owner, room);
  if (sessionConnectionActions.has(key)) return;
  const here = () => owner === agentId() && room === state.room;
  sessionConnectionActions.add(key);
  renderDetails();
  try {
    if (here()) await flushDraft();
    const released = await api(agentPath(owner, "/api/room/" + encodeURI(room) + "/release"),
      {absolute: true, method: "POST", body: {}});
    if (!RELEASE_DONE.has(released.release_state)) {
      throw new Error(released.message || "The old connection has not finished disconnecting. Reconnect was not attempted.");
    }
    manuallyDisconnected.add(key);
    if (reconnect) {
      const result = await api(agentPath(owner, "/api/room/" + encodeURI(room) + "/attach"),
        {absolute: true, method: "POST", body: {}});
      if (!result.room?.ownership?.atlas_owned) {
        throw new Error("The conversation has not confirmed its new connection. Your messages were not resent.");
      }
      manuallyDisconnected.delete(key);
    }
    if (here()) {
      setReceipt(reconnect ? "Reconnected to this conversation. No messages resent."
        : "Disconnected. Your conversation and draft are kept. Choose Reconnect when ready.", "saved");
      await refreshRoomState();
      if (reconnect && here()) await refreshTail();
    }
  } catch (error) {
    if (here()) {
      setReceipt("Could not " + (reconnect ? "reconnect" : "disconnect") + ": " + error.message, "fail");
      await refreshRoomState();
    }
  } finally {
    sessionConnectionActions.delete(key);
    if (here()) renderDetails();
  }
}

async function refreshConnection(button) {
  const owner = agentId();
  const room = state.detail?.id || state.room;
  if (!room) return;
  const target = agentPath(owner, "/api/connection/refresh");
  const titleIcon = button?.id === "btnSessionRefresh";
  const originalText = button?.textContent;
  const stillHere = () => owner === agentId() && room === (state.detail?.id || state.room);
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    if (!titleIcon) button.textContent = "Refreshing…";
  }
  setSendState("Refreshing this session’s connection…", "ok");
  try {
    const payload = await api(target, {absolute: true, method: "POST", body: {room}});
    if (!stillHere()) return;
    const connection = payload.connection || {};
    state.connectionRefreshState = connection.state === "refreshed" || connection.state === "partial"
      ? connection.state : "";
    setReceipt(connection.message || "Connection refresh completed.", connection.state === "refreshed" ? "saved" : "warn");
    await refreshRoomState();
    if (!stillHere()) return;
    if (state.detail?.controllable) await refreshTail();
    if (stillHere() && state.detail?.connection_recovery?.state === "refreshed") {
      setReceipt(connectionSummary(state.detail) + ". No message resent.", "saved");
    }
  } catch (error) {
    if (!stillHere()) return;
    const text = error.code === "connection_busy" || error.code === "refresh_safety_unknown"
      ? error.message : "Could not refresh the connection: " + error.message;
    setReceipt(text, error.code === "connection_busy" ? "warn" : "fail");
  } finally {
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      if (!titleIcon) button.textContent = originalText;
    }
    if (stillHere()) renderDetails();
  }
}
$("#btnSessionRefresh").addEventListener("click", event => refreshConnection(event.currentTarget));
$("#settingsRefresh").addEventListener("click",event=>refreshConnection(event.currentTarget));

// This explicit entry creates a source-scoped project; ordinary blank rooms
// retain their own working folders and permissions. Unknown creation is held.
let customizationRequest = null, customizationBusy = false;
try { customizationRequest = localStorage.getItem('ux46.customize.request'); } catch (error) {}
$('#customizeWorkspace').addEventListener('click', () => {
  $('#workspaceMenu').hidePopover?.(); $('#customizeStart').textContent = customizationRequest ? 'Check previous request' : 'Open source project'; $('#customizeDialog').showModal();
});
$('#customizeClose').addEventListener('click', () => $('#customizeDialog').close());
$('#customizeStart').addEventListener('click', async () => {
  if (customizationBusy) return;
  customizationBusy = true; customizationRequest ||= clientId();
  try { localStorage.setItem('ux46.customize.request',customizationRequest); } catch (error) {}
  $('#customizeStart').disabled = true;
  $('#customizeStatus').textContent = 'Saving a recovery point and opening the source project…';
  try {
    const result = await api('/api/customize/start', {absolute:true, method:'POST', body:{client_id:customizationRequest}});
    if (result.state === 'created' && result.new_room?.id) {
      $('#customizeStatus').textContent = 'Recovery point saved. Use ux46 undo if you want to restore it.';
      $('#customizeDialog').close();
      const agent = result.agent || DEFAULT_AGENT;
      openTab(agent,result.new_room.id,result.new_room); renderTabs();
      await openSession(agent,result.new_room.id,{toTail:true,connect:true});
      customizationRequest = null;
    } else if (result.state === 'failed') {
      $('#customizeStatus').textContent = result.message || 'The source conversation could not be created.';
      customizationRequest = null;
    } else $('#customizeStatus').textContent = result.message || 'The new conversation is not confirmed. Check the workspace before starting another.';
  } catch (error) {
    $('#customizeStatus').textContent = error.status >= 400 && error.status < 500 ? error.message : 'The source conversation is not confirmed. Check the previous request; nothing was retried.';
    if (error.status >= 400 && error.status < 500) customizationRequest = null;
  } finally {
    customizationBusy = false; $('#customizeStart').disabled = false;
    $('#customizeStart').textContent = customizationRequest ? 'Check previous request' : 'Open source project';
    if (!customizationRequest) { try { localStorage.removeItem('ux46.customize.request'); } catch (error) {} }
  }
});

// CLI and UI share the persisted coordinator receipt. Polling reads receipts;
// it never retries a mutation or infers successful recovery from transport.
const workspaceRecovery = {mode: "all", agent: null, id: null, job: null, timer: null, busy: false, until: 0};
function openWorkspaceRecovery(mode = "all", agent = null) {
  clearTimeout(workspaceRecovery.timer);
  Object.assign(workspaceRecovery, {mode, agent, id: null, job: null, busy: false, until: Date.now() + 150000});
  try {
    const saved = JSON.parse(localStorage.getItem("ux46.recovery.request") || "null");
    if (saved && saved.mode === mode && saved.agent === agent) workspaceRecovery.id = saved.id;
  } catch (error) { /* storage is optional; a receipt also survives on the host */ }
  $("#workspaceRecoveryTitle").textContent = mode === "all" ? "Recover UX46" : "Refresh " + (state.agents.find(a => a.id === agent)?.label || "this agent");
  $("#workspaceRecoveryEffect").textContent = mode === "all"
    ? "Restart this installation’s configured UX46 services. Active owned work may be interrupted."
    : "Refresh this agent’s idle owned connections. Busy or unsupported connections are reported.";
  $("#workspaceRecoveryStatus").textContent = "";
  $("#runWorkspaceRecovery").textContent = mode === "all" ? "Recover UX46 — may interrupt owned work" : "Refresh this agent";
  $("#runWorkspaceRecovery").disabled = !!workspaceRecovery.id;
  $("#workspaceRecovery").showModal();
  if (workspaceRecovery.id) void checkWorkspaceRecovery();
}
function renderWorkspaceRecovery(job) {
  const host = $("#workspaceRecoveryStatus"); host.replaceChildren();
  host.appendChild(el("p", {text: job.message || job.state}));
  const rows = [...(job.services || []), ...(job.connections || [])];
  if (rows.length) host.appendChild(el("details", {}, [el("summary", {text: "Recovery results"}),
    ...rows.map(row => el("p", {text: (row.id || row.agent || "Connection") + (row.room ? " · " + row.room : "") + " · " + row.state}))]));
  if (job.state === "partial" && job.mode === "agent") host.appendChild(el("button", {type: "button", class: "linkbtn", text: "Open full UX46 recovery", on: {click: () => { $("#workspaceRecovery").close(); openWorkspaceRecovery(); }}}));
  const terminal = ["complete", "partial", "failed"].includes(job.state);
  $("#runWorkspaceRecovery").disabled = !terminal;
  if (terminal) {
    workspaceRecovery.busy = false;
    try { localStorage.removeItem("ux46.recovery.request"); } catch (error) {}
    workspaceRecovery.id = null;
    state.eventRecovery = true;
  }
}
async function checkWorkspaceRecovery() {
  clearTimeout(workspaceRecovery.timer);
  const id = workspaceRecovery.id;
  if (!id) return;
  try {
    const result = await api("/api/recovery/status?request_id=" + encodeURIComponent(id), {absolute: true});
    if (id !== workspaceRecovery.id) return;
    if (result.job) {
      workspaceRecovery.job = result.job; renderWorkspaceRecovery(result.job);
      if (!workspaceRecovery.id) return;
    } else $("#workspaceRecoveryStatus").textContent = "The recovery receipt is not available yet. No operation has been repeated.";
  } catch (error) {
    if (id !== workspaceRecovery.id) return;
    $("#workspaceRecoveryStatus").textContent = "Waiting for the workspace to return. Recovery runs independently; no input is being replayed.";
  }
  if (Date.now() >= workspaceRecovery.until) {
    $("#workspaceRecoveryStatus").textContent = "Automatic status checks have ended. Check again when ready or use the doctor command. No recovery was repeated.";
    return;
  }
  if ($("#workspaceRecovery").open) workspaceRecovery.timer = setTimeout(checkWorkspaceRecovery, 2000);
}
async function runWorkspaceRecovery() {
  if (workspaceRecovery.busy || workspaceRecovery.id) return;
  const mode = workspaceRecovery.mode, agent = workspaceRecovery.agent, id = clientId();
  Object.assign(workspaceRecovery, {id, busy: true, until: Date.now() + 150000});
  $("#runWorkspaceRecovery").disabled = true;
  try { localStorage.setItem("ux46.recovery.request", JSON.stringify({id, mode, agent})); } catch (error) {}
  try {
    const status = await api("/api/recovery/status", {absolute: true});
    if (id !== workspaceRecovery.id) return;
    const headers = status.recovery_csrf ? {"X-UX46-Recovery-CSRF": status.recovery_csrf} : {};
    const result = await api("/api/recovery/start", {absolute: true, method: "POST", headers, body: {mode, agent, request_id: id}});
    if (id !== workspaceRecovery.id) return;
    if (result.job) { workspaceRecovery.job = result.job; renderWorkspaceRecovery(result.job); }
  } catch (error) {
    if (id !== workspaceRecovery.id) return;
    $("#workspaceRecoveryStatus").textContent = "Recovery could not be confirmed. Check its status or use the doctor command; nothing was retried.";
  }
  if (workspaceRecovery.id) void checkWorkspaceRecovery();
}
$("#openWorkspaceRecovery").addEventListener("click", () => { $("#workspaceMenu").hidePopover?.(); openWorkspaceRecovery(); });
$("#closeWorkspaceRecovery").addEventListener("click", () => $("#workspaceRecovery").close());
$("#workspaceRecovery").addEventListener("close", () => clearTimeout(workspaceRecovery.timer));
$("#checkWorkspaceRecovery").addEventListener("click", () => void checkWorkspaceRecovery());
$("#runWorkspaceRecovery").addEventListener("click", () => void runWorkspaceRecovery());
$("#btnAgentRefresh").addEventListener("click", () => { openWorkspaceRecovery("agent", agentId()); void runWorkspaceRecovery(); });

// Settings opens the same explicit full-recovery control as the workspace menu.
async function loadServiceRecovery() {
  try {
    const status = await api('/api/recovery/status', {absolute:true});
    $('#btnServiceRestart').disabled = !status.supported;
    $('#serviceStatus').textContent = status.supported
      ? 'Recover this installation’s configured services. Active owned work may be interrupted.'
      : 'Use ux46 doctor on the installation’s host.';
  } catch (error) {
    $('#btnServiceRestart').disabled = true;
    $('#serviceStatus').textContent = 'Recovery status is unavailable. Use ux46 doctor on the installation’s host.';
  }
}
$('#btnServiceRestart').addEventListener('click', () => openWorkspaceRecovery());

/* ------------------------------------------------------------------ drawers */
function turnsOf() { return state.items.filter((i) => i.type === "userMessage"); }
/* The newest answer is the one being looked for, so Updates reads newest
   first. My turns stays in the order they were written. */
function updatesOf() {
  const finals = state.items.filter((i) => i.type === "agentMessage" && i.phase === "final_answer");
  return (finals.length ? finals : state.items.filter((i) => i.type === "agentMessage"))
    .slice().reverse();
}
function entriesFor(kind) { return kind === "turns" ? turnsOf() : updatesOf(); }

/* Turns and Updates share one panel; changing which one is showing resets the
   search and asks the server again. Reopening the same one keeps your place. */
function setupNav(kind, force) {
  const changed = force || state.navKind !== kind;
  state.navKind = kind;
  if (!changed) { renderNavList($("#navSearch").value.trim()); return; }
  state.searchHits = [];
  $("#navSearch").value = "";
  $("#navSearch").placeholder = kind === "turns"
    ? "Search everything you have said in this session…"
    : "Search the runtime's final answers…";
  $("#navSub").textContent = kind === "turns"
    ? "Your inputs across this session's native history"
    : "Final answers as reported by the runtime";
  $("#navFoot").textContent = "";
  renderNavList("");
  runNavSearch("");
}

function openNav(kind, trigger) {
  openPanel(kind, trigger);
}

/* The whole native history is searched on the server, so a question from
   twenty minutes ago is findable without paging through every tool result. */
let navSearchTimer = null;
async function runNavSearch(query) {
  const detail = state.detail;
  if (!detail || !detail.controllable) return;
  const kind = state.navKind;
  const roomId = detail.id;
  const seq = state.roomSeq;
  try {
    const payload = await api("/api/room/" + encodeURI(roomId) + "/search?kinds="
      + (kind === "turns" ? "human" : "final")
      + "&limit=60&q=" + encodeURIComponent(query || ""));
    if (stale(seq, roomId) || state.navKind !== kind) return;
    state.searchHits = payload.hits || [];
    $("#navSub").textContent = kind === "turns"
      ? "Your inputs across this session's native history"
      : "Final answers as reported by the runtime";
    $("#navFoot").textContent = payload.source === "unavailable"
      ? (payload.note || "this runtime exposes no searchable history for this session")
      : payload.total + " found in " + payload.searched_items + " native items · "
        + (payload.source === "thread_read" ? "read from the session's turns"
           : payload.source === "thread_read_truncated"
             ? "read from the session's turns, capped" : "read page by page");
    renderNavList(query);
  } catch (error) {
    if (stale(seq, roomId)) return;
    $("#navFoot").textContent = "Search unavailable: " + error.message
      + " — the loaded page is still listed.";
  }
}

function renderNavList(query) {
  const kind = state.navKind;
  const host = $("#navList");
  host.replaceChildren();
  const needle = (query || "").toLowerCase();

  if (state.searchHits.length) {
    for (const hit of state.searchHits) {
      const loaded = state.ids.has(hit.item_id);
      host.appendChild(el("button", {
        class: "entry", type: "button",
        "aria-current": state.anchor === hit.item_id ? "true" : null,
        on: {click: () => { jumpToItem(hit.item_id); dismissOverlay(); }},
      }, [
        el("span", {class: "etime", title: itemHead(hit),
          text: entryWhen(hit, loaded)}),
        el("span", {class: "etext", text: hit.snippet || ""}),
      ]));
    }
    return;
  }

  // Fallback to whatever is loaded when the server search is unavailable.
  const all = entriesFor(kind);
  const list = needle ? all.filter((i) => (i.text || "").toLowerCase().includes(needle)) : all;
  if (!list.length) {
    host.appendChild(el("p", {class: "empty",
      text: needle ? "No match." : "Nothing of that kind in this session yet."}));
    return;
  }
  for (const item of list) {
    const index = all.indexOf(item);
    host.appendChild(el("button", {
      class: "entry", type: "button",
      "aria-current": state.sel && state.sel.kind === kind && state.sel.index === index ? "true" : null,
      on: {click: () => { selectEntry(kind, index); dismissOverlay(); }},
    }, [
      el("span", {class: "etime", title: itemHead(item),
        text: entryWhen(item, true)}),
      el("span", {class: "etext", text: item.text || ""}),
    ]));
  }
}

/* Jump to one stable native id, fetching a page around it when it is outside
   the loaded window. The runtime's own item id is the anchor; nothing is
   matched on text. */
async function jumpToItem(itemId) {
  const detail = state.detail;
  if (!detail) return;
  const roomId = detail.id;
  const seq = state.roomSeq;
  if (state.ids.has(itemId)) {
    state.anchor = itemId;
    revealItem(itemId);
    return;
  }
  setSendState("Loading the page around that message…", "ok");
  try {
    const payload = await api("/api/room/" + encodeURI(roomId)
      + "/history?around=" + encodeURIComponent(itemId));
    if (stale(seq, roomId)) return;
    state.items = payload.items;
    state.ids = new Set(payload.items.map((i) => i.id));
    state.cursor = payload.backwards_cursor || null;
    state.complete = !payload.earlier_available;
    state.anchor = itemId;
    state.sel = null;
    renderStream();
    revealItem(itemId);
    reportDraftState();
  } catch (error) {
    if (stale(seq, roomId)) return;
    setSendState("Could not open that message: " + error.message, "fail");
  }
}

function revealItem(itemId) {
  const node = $("#stream").querySelector('[data-mid="' + CSS.escape(itemId) + '"]');
  if (!node) return;
  for (const other of $("#stream").querySelectorAll(".selected")) other.classList.remove("selected");
  node.classList.add("selected");
  node.scrollIntoView({block: "center"});
  state.following = false;
  const nav = $("#selNav");
  $("#selPos").textContent = "jumped to item " + shortId(itemId);
  $("#selPrev").disabled = true;
  $("#selNext").disabled = true;
  nav.hidden = false;
  syncFloaters();
}

function selectEntry(kind, index) {
  const all = entriesFor(kind);
  if (!all.length) return;
  const i = Math.max(0, Math.min(index, all.length - 1));
  state.sel = {kind, index: i};
  state.anchor = all[i].id;
  renderStream();
  const node = $("#stream").querySelector('[data-mid="' + CSS.escape(all[i].id) + '"]');
  if (node) {
    node.scrollIntoView({block: "center"});
    state.following = distanceFromBottom() < FOLLOW_PX;
  }
  applySelection();
}

function applySelection() {
  const stream = $("#stream");
  for (const node of stream.querySelectorAll(".selected")) node.classList.remove("selected");
  const nav = $("#selNav");
  if (!state.sel) { nav.hidden = true; syncFloaters(); return; }
  const all = entriesFor(state.sel.kind);
  const target = all[state.sel.index];
  if (!target) { state.sel = null; nav.hidden = true; syncFloaters(); return; }
  const node = stream.querySelector('[data-mid="' + CSS.escape(target.id) + '"]');
  if (node) node.classList.add("selected");
  $("#selPos").textContent = (state.sel.kind === "turns" ? "Your turn " : "Update ")
    + (state.sel.index + 1) + " of " + all.length;
  $("#selPrev").disabled = state.sel.index === 0;
  $("#selNext").disabled = state.sel.index === all.length - 1;
  nav.hidden = false;
  syncFloaters();
}


/* ------------------------------------------------- arranging the left list
   The drawer is a place someone works in, so it can be arranged. Projects and
   the sessions inside them can be dragged into an order, and moved between
   Active and Older; a small menu does the same thing for a keyboard or a
   thumb.

   Two rules keep it honest.

   Arranging is arranging. Nothing here pauses a runtime, releases a session,
   archives anything, reassigns a project, edits a transcript, prompts a model
   or touches a credential. "Inactive" is where a thing sits in this list and
   nothing else; the session it names is exactly as alive as it was.

   A choice outlives the clock. Active is worked out from when something was
   last active — but the moment someone says otherwise, that is the answer,
   and no later refresh moves it back. Automatic puts it down again. The same
   goes for order: once a group has been arranged by hand, a newly arrived
   project joins the end rather than shuffling everything to sit where its
   timestamp says it belongs.

   All of it is keyed by agent as well as id, because two agents can hold
   projects and rooms with the same name and mean entirely different work.
   Arranging Agent2's drawer must never move anything in Local's. */

const NAV_SCHEMA = 1;
/* Four days. With a real timestamp that is a rolling ninety-six hours; with a
   reported date and no time on it, it is the last four calendar days, because
   a date string does not know what hour it happened and this must not pretend
   it does. */
const NAV_ACTIVE_MS = 96 * 60 * 60 * 1000;
const NAV_ACTIVE_DAYS = 4;
const ACTIVE = "active";
const INACTIVE = "inactive";

function navKey(agent, id) { return (agent || DEFAULT_AGENT) + "\0" + String(id || ""); }

/* The saved arrangement, read out of the shared workspace state each time so
   a save landing from another device is picked up rather than cached over. */
/* Which arrangement this device follows: the workspace's, or the one it forked
   when somebody ticked "Only this device". */
function navHome() {
  if (deviceOnlyLayout()) return deviceNav();
  return state.desk.raw && typeof state.desk.raw === "object" ? state.desk.raw : null;
}

function navState() {
  const raw = navHome();
  const nav = raw && typeof raw.nav === "object" && raw.nav ? raw.nav : {};
  const read = (list, idField) => {
    const found = new Map();
    for (const entry of Array.isArray(list) ? list : []) {
      if (!entry || typeof entry !== "object") continue;
      const id = String(entry[idField] || "");
      if (!id) continue;
      const group = entry.group === ACTIVE || entry.group === INACTIVE ? entry.group : null;
      const order = Number.isFinite(entry.order) ? Number(entry.order) : null;
      if (group === null && order === null) continue;
      found.set(navKey(entry.agent, id), {
        agent: String(entry.agent || DEFAULT_AGENT), id, group, order,
        // Enough to draw a manually kept row that today's suggestions no
        // longer mention. Never a substitute for the record itself.
        title: typeof entry.title === "string" ? entry.title : "",
        project: typeof entry.project === "string" ? entry.project : "",
      });
    }
    return found;
  };
  return {projects: read(nav.projects, "id"), sessions: read(nav.sessions, "room")};
}

function navRecord(kind, agent, id) {
  const found = navState()[kind === "project" ? "projects" : "sessions"];
  return found.get(navKey(agent, id)) || null;
}

/* Change the arrangement: locally first so the list answers the drag at once,
   then through the workspace's own compare-and-set save, which re-reads the
   latest state and applies the same change to it. Every field this console
   does not know about travels through untouched. */
async function navSave(apply, describe) {
  if (deviceOnlyLayout()) {
    // This device keeps its own list. It is written here and nowhere else.
    apply(deviceNav());
    writeDeviceNav();
    void renderRoomList();
    return {ok: true, local: true};
  }
  const local = state.desk.raw && typeof state.desk.raw === "object" ? state.desk.raw : {};
  apply(local);
  state.desk.raw = local;
  void renderRoomList();
  if (state.desk.available === false) return {ok: false, local: true};
  const result = await saveDesktopState((next) => apply(next), describe);
  if (!result.ok && result.message && !result.conflict) flash(result.message);
  void renderRoomList();
  return result;
}

/* One mutation, written so it can be replayed onto whatever the store hands
   back rather than onto the copy it was composed against. */
function navApply(kind, agent, id, patch, extra) {
  const field = kind === "project" ? "projects" : "sessions";
  const idField = kind === "project" ? "id" : "room";
  const owner = agent || DEFAULT_AGENT;
  return (state_) => {
    if (!state_.nav || typeof state_.nav !== "object") state_.nav = {};
    if (!Number.isFinite(state_.nav.version)) state_.nav.version = NAV_SCHEMA;
    if (!Array.isArray(state_.nav[field])) state_.nav[field] = [];
    const list = state_.nav[field];
    const at = list.findIndex((entry) => entry && String(entry[idField] || "") === String(id)
                              && String(entry.agent || DEFAULT_AGENT) === owner);
    const held = at >= 0 ? list[at] : null;
    const merged = Object.assign({}, held || {}, {agent: owner, [idField]: String(id)},
                                 extra || {}, patch);
    if (merged.group === null) delete merged.group;
    if (merged.order === null) delete merged.order;
    // Nothing left to say about it: drop the record rather than keep an empty.
    const empty = merged.group === undefined && merged.order === undefined;
    if (empty) { if (at >= 0) list.splice(at, 1); return; }
    if (at >= 0) list[at] = merged; else list.push(merged);
  };
}

/* Reordering writes every position in the group at once, so an arrangement is
   a whole answer rather than one item's opinion about where it sits. */
function navApplyOrder(kind, agent, ordered) {
  const field = kind === "project" ? "projects" : "sessions";
  const idField = kind === "project" ? "id" : "room";
  const owner = agent || DEFAULT_AGENT;
  return (state_) => {
    if (!state_.nav || typeof state_.nav !== "object") state_.nav = {};
    if (!Number.isFinite(state_.nav.version)) state_.nav.version = NAV_SCHEMA;
    if (!Array.isArray(state_.nav[field])) state_.nav[field] = [];
    const list = state_.nav[field];
    ordered.forEach((item, index) => {
      const at = list.findIndex((entry) => entry && String(entry[idField] || "") === String(item.id)
                                && String(entry.agent || DEFAULT_AGENT) === owner);
      const held = at >= 0 ? list[at] : null;
      const merged = Object.assign({}, held || {}, {agent: owner, [idField]: String(item.id)},
                                   item.extra || {}, {order: index});
      if (item.group) merged.group = item.group;
      if (at >= 0) list[at] = merged; else list.push(merged);
    });
  };
}

/* ------------------------------------------------------------- when active */
/* The most precise thing the record actually reports, and how precise that
   is. A millisecond stamp is a moment; a date string is a day, and is read as
   a day rather than as midnight. */
function navRecency(row) {
  if (!row) return null;
  for (const key of ["native_updated_ms", "updated_ms"]) {
    const value = Number(row[key]);
    if (Number.isFinite(value) && value > 0) return {exact: true, at: value};
  }
  for (const key of ["native_updated_at", "updated_at"]) {
    const value = row[key];
    // Only a stamp that carries a time is treated as one.
    if (typeof value === "string" && /\d{4}-\d{2}-\d{2}[T ]\d{2}:/.test(value)) {
      const at = Date.parse(value);
      if (Number.isFinite(at)) return {exact: true, at};
    }
  }
  const day = row.last_active;
  if (typeof day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(day)) {
    const at = Date.parse(day + "T00:00:00");
    if (Number.isFinite(at)) return {exact: false, at, day};
  }
  return null;
}

/* Deterministic, and never more confident than the record. A missing or
   unreadable date is not recent; it is unknown, and unknown sits with the
   older work until somebody says otherwise. */
function navIsRecent(row, now) {
  const recency = navRecency(row);
  if (!recency) return false;
  if (recency.exact) return now - recency.at <= NAV_ACTIVE_MS;
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const days = Math.round((startOfToday.getTime() - recency.at) / 86400000);
  return days >= 0 && days < NAV_ACTIVE_DAYS;
}

/* Where something belongs, and on whose authority. */
function navGroupFor(kind, agent, id, row, opts) {
  const record = navRecord(kind, agent, id);
  if (record && record.group) return {group: record.group, manual: true};
  if (opts && opts.open) return {group: ACTIVE, manual: false, why: "open"};
  const now = (opts && opts.now) || Date.now();
  return {group: navIsRecent(row, now) ? ACTIVE : INACTIVE, manual: false};
}

/* Order inside one group: what was arranged by hand first, in that order, and
   everything else after it by how recently it was active. A project that
   arrives tomorrow lands at the end of its group instead of pushing its way
   into the middle of an arrangement somebody made. */
function navSorted(entries) {
  return entries.slice().sort((a, b) => {
    const ao = a.order, bo = b.order;
    if (ao !== null && bo !== null) return ao - bo;
    if (ao !== null) return -1;
    if (bo !== null) return 1;
    const at = (navRecency(a.row) || {}).at || 0;
    const bt = (navRecency(b.row) || {}).at || 0;
    if (at !== bt) return bt - at;
    return String(a.name).localeCompare(String(b.name));
  });
}

/* --------------------------------------------------------------- moving it */
async function navMoveTo(kind, agent, id, group, extra) {
  await navSave(navApply(kind, agent, id, {group}, extra),
                group === ACTIVE ? "Moving that to Active" : "Moving that to Inactive");
}

async function navSetAutomatic(kind, agent, id) {
  await navSave(navApply(kind, agent, id, {group: null, order: null}),
                "Putting that back on automatic");
}

/* A step, expressed as the whole group's new order. */
async function navNudge(kind, agent, group, list, id, delta) {
  const at = list.findIndex((item) => item.id === id);
  const to = at + delta;
  if (at < 0 || to < 0 || to >= list.length) return;
  const ordered = list.slice();
  ordered.splice(to, 0, ordered.splice(at, 1)[0]);
  await navSave(navApplyOrder(kind, agent, ordered.map((item) => ({
    id: item.id, group: item.manual ? item.group : null, extra: item.extra,
  }))), "Reordering");
}

async function navDropInto(kind, agent, group, list, movedId, beforeId, extra) {
  const without = list.filter((item) => item.id !== movedId);
  const at = beforeId ? without.findIndex((item) => item.id === beforeId) : without.length;
  const moved = list.find((item) => item.id === movedId)
    || {id: movedId, extra, group, manual: true};
  without.splice(at < 0 ? without.length : at, 0, moved);
  await navSave((state_) => {
    // The group first, so an item arriving from the other side is placed
    // before its position in this one is written.
    navApply(kind, agent, movedId, {group}, extra)(state_);
    navApplyOrder(kind, agent, without.map((item) => ({
      id: item.id, group: item.id === movedId ? group : (item.manual ? item.group : null),
      extra: item.id === movedId ? extra : item.extra,
    })))(state_);
  }, "Moving that");
}

/* ------------------------------------------------------------------ drag */
const NAV_MIME = "application/x-ux46-nav";
let navDrag = null;

function navDragStart(event, kind, id, group) {
  navDrag = {kind, id, group, agent: browseAgent()};
  try {
    event.dataTransfer.setData(NAV_MIME, kind + " " + id);
    event.dataTransfer.effectAllowed = "move";
  } catch (e) { /* a browser that will not carry it still drags locally */ }
  document.body.classList.add("nav-dragging");
}

function navDragEnd() {
  navDrag = null;
  document.body.classList.remove("nav-dragging");
  for (const node of document.querySelectorAll(".navdrop, .navover")) {
    node.classList.remove("navdrop", "navover");
  }
}

/* A drop target only answers a drag of its own kind, so a project cannot be
   dropped inside a project and a file dropped anywhere is still an upload. */
function navAccepts(kind) {
  return navDrag && navDrag.kind === kind && navDrag.agent === browseAgent();
}

function navDropZone(node, kind, onDrop, marker) {
  node.addEventListener("dragover", (event) => {
    if (!navAccepts(kind)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "move";
    node.classList.add(marker || "navdrop");
  });
  node.addEventListener("dragleave", () => node.classList.remove(marker || "navdrop"));
  node.addEventListener("drop", (event) => {
    if (!navAccepts(kind)) return;
    event.preventDefault();
    event.stopPropagation();     // never an upload
    const held = navDrag;
    navDragEnd();
    void onDrop(held);
  });
  return node;
}

/* ------------------------------------------------------------------- menu */
let navMenuNode = null;

function navMenu() {
  if (!navMenuNode) {
    navMenuNode = el("div", {class: "navmenu", role: "menu", hidden: true,
                             "aria-label": "Arrange this item"});
    document.body.appendChild(navMenuNode);
    document.addEventListener("pointerdown", (event) => {
      if (!navMenuNode.hidden && !navMenuNode.contains(event.target)
          && !event.target.closest(".navmore")) closeNavMenu();
    });
  }
  return navMenuNode;
}

function closeNavMenu(restore) {
  const host = navMenu();
  if (host.hidden) return;
  host.hidden = true;
  host.replaceChildren();
  if (restore && navMenuAnchor && navMenuAnchor.isConnected) navMenuAnchor.focus();
  navMenuAnchor = null;
}

let navMenuAnchor = null;

/* Everything a drag can do, said in words. It lives behind one quiet control
   so the list stays a list. */
function openNavMenu(anchor, entry) {
  const host = navMenu();
  host.replaceChildren();
  host.hidden = false;
  navMenuAnchor = anchor;
  const item = (label, disabled, run) => el("button", {
    class: "navmenu-item", type: "button", role: "menuitem", disabled: Boolean(disabled),
    on: {click: () => { closeNavMenu(); void run(); }},
  }, [el("span", {text: label})]);

  host.appendChild(el("p", {class: "navmenu-head", text: entry.name}));
  // Where it sits now, not where it sat when the row was drawn: a drop that
  // landed between the two would otherwise grey out a move that is available.
  const list = entry.listOf ? entry.listOf() : entry.list;
  const at = list.findIndex((row) => row.id === entry.id);
  host.appendChild(item("Move up", at <= 0,
    () => navNudge(entry.kind, entry.agent, entry.group, list, entry.id, -1)));
  host.appendChild(item("Move down", at < 0 || at >= list.length - 1,
    () => navNudge(entry.kind, entry.agent, entry.group, list, entry.id, 1)));
  host.appendChild(item(entry.group === ACTIVE ? "Move to inactive" : "Move to active", false,
    () => navMoveTo(entry.kind, entry.agent, entry.id,
                    entry.group === ACTIVE ? INACTIVE : ACTIVE, entry.extra)));
  host.appendChild(item("Automatic", !entry.manual,
    () => navSetAutomatic(entry.kind, entry.agent, entry.id)));
  host.appendChild(el("p", {class: "navmenu-note",
    text: "Organize your list here. Use session controls to pause work."}));

  const box = anchor.getBoundingClientRect();
  host.style.left = Math.max(8, Math.min(box.left - 150, window.innerWidth - 232)) + "px";
  host.style.top = Math.min(box.bottom + 6, window.innerHeight - 220) + "px";
  const first = host.querySelector(".navmenu-item:not([disabled])");
  if (first) first.focus();
}

/* The one control that opens it, and the handle that starts a drag. */
function navControls(entry) {
  const handle = el("span", {class: "navgrip", "aria-hidden": "true", text: "⠿"});
  const more = el("button", {
    class: "navmore", type: "button", "aria-haspopup": "menu",
    title: "Arrange " + entry.name,
    "aria-label": "Arrange " + entry.name,
    on: {click: (event) => { event.stopPropagation(); openNavMenu(event.currentTarget, entry); },
         pointerdown: (event) => event.stopPropagation()},
  }, [el("span", {"aria-hidden": "true", text: "⋯"})]);
  return {handle, more};
}

/* -------------------------------------------------------------- rooms sheet */
let roomSearchTimer = null;
const PAGE = 25;

/* The working set, not the catalogue.

   A project opens on the one or two conversations a person would plausibly
   resume, plus whatever they pinned and whatever is open right now. Records
   that point at the same native conversation are one row with its other names
   listed as aliases, and conversations the runtime proved were spawned by an
   agent are behind an explicit Agent work view. Nothing is deleted or
   reclassified: everything remains one query away in history and search. */
async function loadWorkspace(force) {
  if (state.workspace && !force) return state.workspace;
  const payload = await browseApi("/api/workspace");
  state.workspace = payload;
  state.prefs = payload.prefs || {};
  for (const project of payload.projects) {
    for (const row of project.pinned.concat(project.suggested)) state.rooms.set(row.id, row);
  }
  return payload;
}

async function renderRoomList() {
  const query = $("#roomSearch").value.trim();
  const host = $("#roomList");
  // Search still reaches everything, arranged or not, active or older.
  if (query) return renderRoomSearch(query, host);

  let payload;
  try { payload = await loadWorkspace(); }
  catch (error) { host.replaceChildren(el("p", {class: "empty", text: error.message})); return; }
  const agent = browseAgent();
  const now = Date.now();
  const openProject = state.room && agent === agentId()
    ? (state.rooms.get(state.room) || {}).project_id : "";

  const groups = {[ACTIVE]: [], [INACTIVE]: []};
  for (const project of payload.projects) {
    if (!project.record_total) continue;              // registered, nothing recorded yet
    const record = navRecord("project", agent, project.id);
    const placed = navGroupFor("project", agent, project.id, project,
                               {now, open: project.id === openProject});
    groups[placed.group].push({
      kind: "project", id: project.id, agent, name: project.name, row: project,
      group: placed.group, manual: placed.manual,
      order: record && Number.isFinite(record.order) ? record.order : null,
      extra: {title: project.name},
    });
  }
  for (const key of [ACTIVE, INACTIVE]) groups[key] = navSorted(groups[key]);
  // Read back at drop time, so a drop lands in the list as it is now rather
  // than as it was when the row being dragged was drawn.
  navGroups.project = groups;

  host.replaceChildren();
  host.appendChild(navGroupNode(ACTIVE, "Active", groups[ACTIVE], {
    empty: "Nothing has been active in the last four days. Drop a project here to keep it.",
  }));
  host.appendChild(navGroupNode(INACTIVE, "Inactive", groups[INACTIVE], {
    empty: "No inactive projects. Drop a project here to tuck it away.",
    collapsible: true,
  }));

  const shown = groups[ACTIVE].length + groups[INACTIVE].length;
  $("#roomsSub").textContent = shown + (shown === 1 ? " project" : " projects")
    + " · " + groups[ACTIVE].length + " active";
  $("#roomFoot").textContent = "Drag to arrange · ⋯ for more options. Search includes everything.";
}

/* One of the two halves of the drawer. It accepts a drop whether or not it has
   anything in it, and the header accepts one while it is shut, so tucking the
   last project away and fetching it back are both one gesture. */
function navGroupNode(group, label, entries, opts) {
  const options = opts || {};
  const open = group === ACTIVE || state.openProjects.has("::older");
  const node = el("div", {class: "navgroup" + (group === INACTIVE ? " older" : ""),
                          data: {group}});
  const head = el(options.collapsible ? "button" : "div", Object.assign({
    class: "navhead",
  }, options.collapsible ? {
    type: "button", "aria-expanded": String(open),
    title: "Projects tucked away from your active list.",
    on: {click: () => {
      if (state.openProjects.has("::older")) state.openProjects.delete("::older");
      else state.openProjects.add("::older");
      void renderRoomList();
    }},
  } : {}), [
    options.collapsible
      ? el("span", {class: "chev", "aria-hidden": "true", text: open ? "▾" : "▸"}) : null,
    el("span", {class: "navhead-name", text: label}),
    el("span", {class: "navhead-count", text: String(entries.length)}),
  ]);
  // A shut header is still a target, so the list never has to be opened first.
  navDropZone(head, "project",
              (held) => navDropInto("project", held.agent, group,
                                    navGroupList("project", group), held.id, null, held.extra),
              "navover");
  node.appendChild(head);

  if (!open) return node;
  const body = el("div", {class: "navbody"});
  navDropZone(body, "project",
              (held) => navDropInto("project", held.agent, group,
                                    navGroupList("project", group), held.id, null, held.extra));
  if (!entries.length) {
    body.appendChild(el("p", {class: "navempty", text: options.empty}));
  }
  for (const entry of entries) body.appendChild(projectNode(entry, entries));
  node.appendChild(body);
  return node;
}

/* The list a drop is being placed into, read back at drop time rather than
   captured when the row was drawn. */
let navGroups = {project: {}, session: {}};
function navGroupList(kind, group, projectId) {
  const held = kind === "project" ? navGroups.project : (navGroups.session[projectId] || {});
  return (held[group] || []).slice();
}

function projectNode(entry, siblings) {
  const project = entry.row;
  const open = state.openProjects.has(project.id);
  const group = el("div", {class: "pgroup" + (entry.group === INACTIVE ? " dim" : ""),
                           draggable: "true", data: {project: project.id}});
  group.addEventListener("dragstart", (event) => {
    event.stopPropagation();
    navDragStart(event, "project", project.id, entry.group);
  });
  group.addEventListener("dragend", navDragEnd);
  // Dropping onto a project puts the dragged one in front of it.
  navDropZone(group, "project", (held) => {
    if (held.id === project.id) return Promise.resolve();
    return navDropInto("project", held.agent, entry.group,
                       navGroupList("project", entry.group), held.id, project.id, held.extra);
  });

  const controls = navControls(Object.assign({}, entry, {
    list: siblings, listOf: () => navGroupList("project", entry.group)}));
  const head = el("div", {class: "phead-wrap"});
  head.appendChild(controls.handle);
  head.appendChild(el("button", {
    class: "phead", type: "button", "aria-expanded": String(open),
    on: {click: () => toggleProject(project.id)},
  }, [
    el("span", {class: "chev", "aria-hidden": "true", text: open ? "▾" : "▸"}),
    el("span", {class: "pmain"}, [
      el("span", {class: "pname", text: project.name}),
      el("span", {class: "pcount", text: navWhen(project, entry)}),
    ]),
  ]));
  head.appendChild(controls.more);
  group.appendChild(head);

  if (open) {
    const body = el("div", {class: "psessions", data: {project: project.id}});
    group.appendChild(body);
    renderProjectWorkingSet(project, body);
  }
  return group;
}

/* When it was last active, said with exactly the confidence the record has,
   plus a quiet word when a person put it where it is. */
function navWhen(row, entry) {
  const recency = navRecency(row);
  const said = !recency ? "no dated activity recorded"
    : recency.exact ? "last active " + when(recency.at / 1000)
    : "last active " + recency.day + (row.recency_source === "record" ? " (as recorded)" : "");
  return entry && entry.manual ? said + " · kept here" : said;
}

function toggleProject(projectId) {
  if (state.openProjects.has(projectId)) state.openProjects.delete(projectId);
  else state.openProjects.add(projectId);
  renderRoomList();
}

function projectView(projectId) {
  let view = state.projectViews.get(projectId);
  if (!view) {
    view = {mode: "", page: null, loading: false, error: ""};
    state.projectViews.set(projectId, view);
  }
  return view;
}

/* The open room always has a place, even when it is not a suggestion: the
   person is in it, so hiding it from its own project would be a lie. */
function currentRoomRow(project, alreadyShown) {
  if (!state.room || browseAgent() !== agentId()) return null;
  const room = state.rooms.get(state.room);
  if (!room || room.project_id !== project.id) return null;
  if (alreadyShown.has(room.id)) return null;
  return Object.assign({}, room, {reason: "open", reason_label: "open right now"});
}

function renderProjectWorkingSet(project, host) {
  host.replaceChildren();
  const agent = browseAgent();
  const now = Date.now();
  const rows = project.pinned.concat(project.suggested);
  const seen = new Set(rows.map((row) => row.id));
  const current = currentRoomRow(project, seen);
  if (current) { rows.unshift(current); seen.add(current.id); }

  /* A session somebody chose to keep here stays here, even when today's
     suggestions no longer mention it. The choice recorded its identity, so it
     can be drawn from that and from whatever this console already holds — no
     new listing is fetched to find it. */
  for (const record of navState().sessions.values()) {
    if (record.agent !== agent || record.project !== project.id) continue;
    if (seen.has(record.id) || !record.group) continue;
    seen.add(record.id);
    const held = state.rooms.get(record.id);
    rows.push(held || {
      id: record.id, project_id: project.id, project_name: project.name,
      session: record.id.split("/")[1] || record.id,
      title: record.title || record.id.split("/")[1] || record.id,
      last_active: "", recency_source: "none", capability_short: "",
      capability_label: "kept here", controllable: false, kept: true,
    });
  }

  const groups = {[ACTIVE]: [], [INACTIVE]: []};
  for (const room of rows) {
    const record = navRecord("session", agent, room.id);
    const placed = navGroupFor("session", agent, room.id, room,
                               {now, open: room.id === state.room && agent === agentId()});
    groups[placed.group].push({
      kind: "session", id: room.id, agent, name: room.title || room.session, row: room,
      group: placed.group, manual: placed.manual,
      order: record && Number.isFinite(record.order) ? record.order : null,
      extra: {title: room.title || room.session, project: project.id},
    });
  }
  for (const key of [ACTIVE, INACTIVE]) groups[key] = navSorted(groups[key]);
  navGroups.session[project.id] = groups;

  const list = (group, entries) => {
    const body = el("div", {class: "navsessions", data: {group}});
    navDropZone(body, "session", (held) =>
      navDropInto("session", held.agent, group,
                  navGroupList("session", group, project.id), held.id, null, held.extra));
    if (!entries.length) {
      body.appendChild(el("p", {class: "navempty small",
        text: group === ACTIVE ? "Nothing active here. Drop a session to keep it."
                               : "Drop a session here to tuck it away."}));
    }
    for (const entry of entries) body.appendChild(sessionNode(entry, entries, project, group));
    return body;
  };

  if (!rows.length) {
    host.appendChild(el("p", {class: "psub", text: "Nothing suggested here yet."}));
  }
  host.appendChild(list(ACTIVE, groups[ACTIVE]));

  const olderKey = project.id + "::older";
  const olderOpen = state.openProjects.has(olderKey);
  if (groups[INACTIVE].length || navDrag) {
    const head = el("button", {
      class: "navsub", type: "button", "aria-expanded": String(olderOpen),
      title: "Sessions tucked away from your active list.",
      on: {click: () => {
        if (olderOpen) state.openProjects.delete(olderKey);
        else state.openProjects.add(olderKey);
        void renderRoomList();
      }},
    }, [
      el("span", {class: "chev", "aria-hidden": "true", text: olderOpen ? "▾" : "▸"}),
      el("span", {text: "Inactive"}),
      el("span", {class: "navhead-count", text: String(groups[INACTIVE].length)}),
    ]);
    navDropZone(head, "session", (held) =>
      navDropInto("session", held.agent, INACTIVE,
                  navGroupList("session", INACTIVE, project.id), held.id, null, held.extra),
      "navover");
    host.appendChild(head);
    if (olderOpen) host.appendChild(list(INACTIVE, groups[INACTIVE]));
  }

  const view = projectView(project.id);
  const acts = el("div", {class: "pacts"});
  const more = Math.max(0, project.conversation_total - project.agent_work_total);
  acts.appendChild(el("button", {
    class: "linkbtn", type: "button", "aria-expanded": String(view.mode === "more"),
    text: (view.mode === "more" ? "Hide history" : "History") + " · " + more,
    on: {click: () => setProjectView(project, "more")},
  }));
  if (project.agent_work_total) {
    acts.appendChild(el("button", {
      class: "linkbtn", type: "button", "aria-expanded": String(view.mode === "agents"),
      text: (view.mode === "agents" ? "Hide agent work" : "Agent work") + " · "
        + project.agent_work_total,
      on: {click: () => setProjectView(project, "agents")},
    }));
  }
  host.appendChild(acts);
  if (project.hidden_total) {
    host.appendChild(el("p", {class: "psub",
      text: project.hidden_total
        + (project.hidden_total === 1 ? " conversation is" : " conversations are")
        + " hidden from suggestions here. They still appear in history and search, "
        + "where you can restore them."}));
  }
  if (view.mode) renderProjectPage(project, host);
}

function sessionNode(entry, siblings, project, group) {
  const wrap = el("div", {class: "navsession" + (group === INACTIVE ? " dim" : ""),
                          draggable: "true", data: {room: entry.id}});
  wrap.addEventListener("dragstart", (event) => {
    event.stopPropagation();
    navDragStart(event, "session", entry.id, group);
  });
  wrap.addEventListener("dragend", navDragEnd);
  navDropZone(wrap, "session", (held) => {
    if (held.id === entry.id) return Promise.resolve();
    return navDropInto("session", held.agent, group,
                       navGroupList("session", group, project.id), held.id, entry.id, held.extra);
  });
  const controls = navControls(Object.assign({}, entry, {
    list: siblings, listOf: () => navGroupList("session", group, project.id)}));
  wrap.appendChild(controls.handle);
  wrap.appendChild(roomRow(entry.row, {kept: entry.row.kept, manual: entry.manual}));
  wrap.appendChild(controls.more);
  return wrap;
}

function setProjectView(project, mode) {
  const view = projectView(project.id);
  view.mode = view.mode === mode ? "" : mode;
  view.page = null;
  view.error = "";
  renderRoomList();
}

async function renderProjectPage(project, host) {
  const view = projectView(project.id);
  const body = el("div", {class: "phistory"});
  host.appendChild(body);
  const agents = view.mode === "agents";
  body.appendChild(el("p", {class: "psub", text: agents
    ? "Conversations the runtime recorded as spawned by an agent, newest first."
    : "Every conversation recorded here, newest first. One row is one native "
      + "conversation; its other records are listed as aliases."}));
  if (!view.page && !view.loading) {
    view.loading = true;
    try {
      view.page = await loadRooms("", {
        project: project.id, limit: PAGE, offset: 0, group: true,
        workers: agents ? "only" : "0", complete: true,
      });
    } catch (error) {
      view.error = error.message;
    } finally { view.loading = false; }
    return renderRoomList();
  }
  if (view.error) {
    body.appendChild(el("p", {class: "psub", text: "Could not list them: " + view.error}));
    return;
  }
  if (!view.page) { body.appendChild(el("p", {class: "psub", text: "Loading…"})); return; }
  for (const room of view.page.rooms) body.appendChild(roomRow(room));
  const left = view.page.total - view.page.rooms.length;
  if (left > 0) {
    body.appendChild(el("button", {
      class: "linkbtn loadmore", type: "button",
      text: "Load " + Math.min(PAGE, left) + " older · " + left + " left",
      on: {click: async (event) => {
        event.currentTarget.disabled = true;
        const next = await loadRooms("", {
          project: project.id, limit: PAGE, offset: view.page.rooms.length, group: true,
          workers: agents ? "only" : "0", complete: true,
        });
        view.page = Object.assign({}, next, {rooms: view.page.rooms.concat(next.rooms)});
        renderRoomList();
      }},
    }));
  } else if (view.page.total) {
    body.appendChild(el("p", {class: "psub", text: "All " + view.page.total + " listed."}));
  } else {
    body.appendChild(el("p", {class: "psub", text: "Nothing recorded here."}));
  }
}

async function renderRoomSearch(query, host) {
  let payload;
  try {
    payload = await loadRooms(query, {
      limit: 60, group: true,
      workers: $("#fltWorkers").checked ? "1" : "0",
      complete: $("#fltComplete").checked,
    });
  } catch (error) {
    host.replaceChildren(el("p", {class: "empty", text: error.message}));
    return;
  }
  host.replaceChildren();
  for (const room of payload.rooms) host.appendChild(roomRow(room, {project: true}));
  if (!payload.rooms.length) host.appendChild(el("p", {class: "empty", text: "No room matches."}));
  $("#roomsSub").textContent = payload.total + " conversations match · "
    + payload.returned + " shown";
  $("#roomFoot").textContent = payload.truncated
    ? "Refine the search, or clear it to go back to your working set."
    : "All matching conversations are listed.";
}

/* One row is one conversation: what it is, when it was really last active,
   why it is on screen, and what this person chose to do about it. */
function roomRow(room, opts) {
  opts = opts || {};
  // A preference belongs to the conversation, so a row reads the flags of
  // every record behind it, not only the one it is named after.
  const records = [room.id, ...(room.aliases || [])];
  const prefFor = (key) => (state.prefs && state.prefs[key]) || {};
  const pinned = room.pinned !== undefined
    ? room.pinned : records.some((key) => prefFor(key).pinned);
  const hidden = room.hidden !== undefined
    ? room.hidden : records.some((key) => prefFor(key).hidden);
  const bits = [];
  if (opts.project) bits.push(room.project_name);
  if (room.last_active) {
    bits.push(room.last_active + (room.recency_source === "record" ? " (as recorded)" : ""));
  } else bits.push("no date recorded");
  bits.push(room.capability_short || room.capability_label);
  if (room.status && room.status !== "active") bits.push(room.status);
  if (room.worker) bits.push("agent work");
  else if (room.native_role === "unknown") bits.push("origin unknown");
  if (room.alias_count) {
    bits.push(room.alias_count + (room.alias_count === 1 ? " other record" : " other records"));
  }
  const open = el("button", {
    class: "roomrow", type: "button",
    "aria-current": room.id === state.room ? "true" : null,
    title: (room.aliases && room.aliases.length) ? "also recorded as " + room.aliases.join(", ") : null,
    on: {click: () => openSession(browseAgent(), room.id, {toTail: true, connect: true})},
  }, [
    el("span", {class: "rn", text: room.title || room.session}),
    room.reason_label ? el("span", {class: "rwhy", text: room.reason_label}) : null,
    el("span", {class: "rm" + (room.controllable ? " ok" : ""), text: bits.join(" · ")}),
  ]);
  const acts = el("div", {class: "rracts"}, [
    el("button", {
      class: "pinbtn" + (pinned ? " on" : ""), type: "button",
      "aria-pressed": String(pinned), title: pinned ? "Unpin from this project" : "Pin to this project",
      text: pinned ? "★" : "☆",
      on: {click: () => setRoomPref(room.id, {pinned: !pinned})},
    }),
    el("button", {
      class: "pinbtn" + (hidden ? " on" : ""), type: "button",
      "aria-pressed": String(hidden),
      title: hidden ? "Restore to suggestions" : "Hide from suggestions",
      text: hidden ? "◇" : "◆",
      on: {click: () => setRoomPref(room.id, {hidden: !hidden})},
    }),
  ]);
  return el("div", {class: "rrow" + (hidden ? " dim" : "")}, [open, acts]);
}

async function setRoomPref(roomId, patch) {
  try {
    await browseApi("/api/room/" + encodeURI(roomId) + "/pref", {method: "POST", body: patch});
  } catch (error) {
    flash("Could not save that: " + error.message);
    return;
  }
  await loadWorkspace(true);
  for (const view of state.projectViews.values()) view.page = null;
  renderRoomList();
}

function invalidateRoomPages() {
  state.projectViews.clear();
  state.workspace = null;
}

/* --------------------------------------------------------------- the board */
async function renderBoard() {
  const host = $("#ccBody");
  let payload;
  try { payload = await api("/api/attention"); } catch (error) {
    host.replaceChildren(el("p", {class: "empty", text: "Cannot read attention: " + error.message}));
    return;
  }
  state.attention = payload;
  state.approvals = payload.approvals || [];
  host.replaceChildren();
  host.appendChild(el("h2", {text: "Command center"}));
  host.appendChild(el("p", {class: "lede",
    text: "Live runtime requests first, then what rooms reported. Reported state is what a "
      + "checkpoint said at its time, not a verification of the runtime now."}));

  const approvals = payload.approvals || [];
  const section = el("section", {class: "cc-sec"});
  section.appendChild(el("h3", {text: "The runtime is waiting on you · " + approvals.length}));
  if (!approvals.length) {
    section.appendChild(el("p", {class: "empty", text: "No native session is blocked on you right now."}));
  }
  for (const approval of approvals) {
    const card = el("div", {class: "decide"});
    card.appendChild(el("p", {class: "decide-kicker", text: approval.kind.replace("_", " ")}));
    card.appendChild(el("h3", {class: "decide-lead",
      text: approval.kind.startsWith("command") ? "Approve a command before it runs"
        : approval.kind.startsWith("file_change") ? "Approve a file change before it is written"
        : approval.kind === "permissions" ? "Grant a permission for this one request"
        : "Answer the runtime's question"}));
    const params = approval.params || {};
    if (params.command) card.appendChild(el("pre", {class: "out", text: params.command}));
    if ((params.paths || []).length) {
      card.appendChild(el("pre", {class: "out", text: params.paths.join("\n")}));
    }
    for (const question of params.questions || []) {
      card.appendChild(el("p", {class: "decide-why", text: question.title}));
    }
    if (params.reason) card.appendChild(el("p", {class: "decide-why", text: params.reason}));
    card.appendChild(el("p", {class: "decide-attrib",
      text: (approval.room || "unmapped room") + " · thread " + shortId(approval.thread_id)
        + " · turn " + shortId(approval.turn_id)}));
    const acts = el("div", {class: "decide-acts"});
    if (approval.kind === "user_input") {
      for (const question of params.questions || []) {
        for (const option of question.options.length ? question.options : ["ok"]) {
          acts.appendChild(el("button", {class: "primary", type: "button", text: option,
            on: {click: () => answerApproval(approval, "answer", {[question.id]: option})}}));
        }
      }
    } else {
      acts.appendChild(el("button", {class: "primary", type: "button", text: "Accept once",
        on: {click: () => answerApproval(approval, "accept")}}));
      acts.appendChild(el("button", {class: "danger", type: "button", text: "Decline",
        on: {click: () => answerApproval(approval, "decline")}}));
    }
    if (approval.room) {
      acts.appendChild(el("button", {class: "linkbtn", type: "button", text: "Open that room",
        on: {click: () => selectRoom(approval.room, {connect: true})}}));
    }
    card.appendChild(acts);
    section.appendChild(card);
  }
  host.appendChild(section);

  const unsettled = payload.unsettled || [];
  if (unsettled.length) {
    const sec = el("section", {class: "cc-sec"});
    sec.appendChild(el("h3", {text: "Your inputs with an unfinished outcome · " + unsettled.length}));
    const list = el("div", {class: "rowlist"});
    for (const submission of unsettled) {
      list.appendChild(el("div", {class: "qrow"}, [
        el("div", {class: "rmain"}, [
          el("div", {class: "rhead"}, [
            el("span", {class: "rname", text: submission.room}),
            el("span", {class: "robj", text: submission.status}),
          ]),
          el("span", {class: "ract", text: submission.body.slice(0, 160)}),
          el("p", {class: "rmeta", text: submission.meaning + " · " + when(submission.created_at)}),
        ]),
        el("button", {class: "linkbtn", type: "button", text: "Open room",
          on: {click: () => selectRoom(submission.room, {connect: true})}}),
      ]));
    }
    sec.appendChild(list);
    host.appendChild(sec);
  }

  const needs = payload.needs_person || [];
  const sec = el("section", {class: "cc-sec"});
  sec.appendChild(el("h3", {text: "Rooms that reported needing a person · "
    + payload.needs_person_total}));
  sec.appendChild(el("p", {class: "lede",
    text: "Saved checkpoints, each dated and attributed. A report is what an agent said "
      + "at that time, not a runtime waiting on you now, and UX46 does not mark one "
      + "resolved on its own."}));
  if (!needs.length) sec.appendChild(el("p", {class: "empty", text: "No current room reported needing you."}));
  const list = el("div", {class: "rowlist"});
  for (const room of needs) list.appendChild(reportedRow(room));
  sec.appendChild(list);
  host.appendChild(sec);

  const older = payload.reported_suppressed || [];
  if (older.length) {
    const osec = el("section", {class: "cc-sec"});
    osec.appendChild(el("h3", {text: "Older and agent-work reports · "
      + payload.reported_suppressed_total}));
    osec.appendChild(el("p", {class: "lede",
      text: "These checkpoints also asked for a person, but they belong to finished work, "
        + "to agent work, or to more than " + (payload.stale_after_days || 14)
        + " days ago. They are kept here, unchanged and still open-able, rather than "
        + "counted as something waiting on you."}));
    const olist = el("div", {class: "rowlist"});
    for (const room of older) olist.appendChild(reportedRow(room));
    osec.appendChild(olist);
    host.appendChild(osec);
  }

  const watching = payload.watching || [];
  if (watching.length) {
    const wsec = el("section", {class: "cc-sec"});
    wsec.appendChild(el("h3", {text: "Waiting on something external · " + payload.watching_total}));
    const wlist = el("div", {class: "rowlist"});
    for (const room of watching) {
      const cp = room.checkpoint || {};
      wlist.appendChild(el("div", {class: "qrow"}, [
        el("div", {class: "rmain"}, [
          el("div", {class: "rhead"}, [el("span", {class: "rname",
            text: room.project_name + " · " + room.session})]),
          el("span", {class: "ract", text: "reported waiting on " + (cp.need || "something external")}),
          el("p", {class: "rmeta", text: "no human action requested · at " + (cp.at || "unknown")}),
        ]),
        el("button", {class: "linkbtn", type: "button", text: "Open room",
          on: {click: () => selectRoom(room.id, {connect: true})}}),
      ]));
    }
    wsec.appendChild(wlist);
    host.appendChild(wsec);
  }
  updateNeedsCount();
}

/* One reported checkpoint, said as a report: who, when, and what they asked
   for — never as a verified live state. */
function reportedRow(room) {
  const cp = room.checkpoint || {};
  const age = room.reported_age_days;
  const meta = [cp.next || "no next action recorded",
                "reported at " + (cp.at || "unknown")];
  if (age !== null && age !== undefined) {
    meta.push(age < 1 ? "today" : Math.round(age) + " days ago");
  }
  if (room.suppressed_reason) meta.push("not counted: " + room.suppressed_reason);
  meta.push(room.capability_label);
  return el("div", {class: "qrow"}, [
    el("div", {class: "rmain"}, [
      el("div", {class: "rhead"}, [
        el("span", {class: "rname", text: room.project_name + " · " + room.session}),
        el("span", {class: "robj", text: room.title}),
      ]),
      el("span", {class: "ract", text: "reported " + (cp.state || "?") + " / need "
        + (cp.need || "?") + " by " + (cp.reporter || "?")}),
      el("p", {class: "rmeta", text: meta.join(" · ")}),
    ]),
    el("button", {class: "linkbtn", type: "button", text: "Open room",
      on: {click: () => selectRoom(room.id, {connect: true})}}),
  ]);
}

function updateNeedsCount() {
  const badge = $("#needsCount");
  const approvals = (state.attention && state.attention.approvals ? state.attention.approvals.length
    : state.approvals.length);
  badge.textContent = String(approvals);
  badge.hidden = approvals === 0;
  $("#btnBoard").setAttribute("aria-label", approvals
    ? "Command center · " + approvals + " runtime requests waiting" : "Command center");
}

/* ------------------------------------------------------- attention register
   The right-hand register from the UX46 package, over the read-only APIs the
   console already has. For every configured agent it reads that agent's own
   `/api/attention` and `/api/workspace` — the local one directly, a remote one
   through its `/api/agents/<id>` prefix — and turns what they report into one
   compact row per conversation: project, the condition that was actually
   reported, the concrete step, what comes next, and how old the observation
   is.

   Three rules hold the whole thing up.

   Nothing is invented. A row's condition comes from a live approval, a saved
   checkpoint, or a workspace reason the server wrote; there is no derived
   progress, no summary, no model call, and a reachable agent is never drawn as
   an agent doing work.

   Nothing is answered for. An agent that does not report attention — the Agent3
   adapter reads no checkpoint and says so — is shown as not reporting, never
   as reporting zero. An unreachable agent says it is unreachable. Neither is
   allowed to delay or replace what the other agents already returned.

   Nothing is confused. Two agents can hold identically named rooms, so every
   row, count and action is keyed by agent *and* room, and acting on one
   switches to that agent before opening it. */

const ATTENTION_REFRESH_MS = 20000;
let attentionTimer = null;
let attentionGen = 0;

/* Rank orders the register and decides nothing else. Only a live approval and
   a reported request for a person are counted as needing one. */
const NEEDS_RANK = 3;

function attentionAgents() {
  const listed = state.agents || [];
  if (listed.length) return listed;
  return [{id: DEFAULT_AGENT, label: agentLabel(DEFAULT_AGENT), kind: "local"}];
}

/* One agent's read path. The local agent keeps the plain routes it has always
   used; a remote one is addressed through its own prefix. Both are absolute,
   because this panel reads every agent at once and must not be re-pointed by
   whichever one happens to be selected. */
function agentPath(id, path) {
  return id === DEFAULT_AGENT ? path : "/api/agents/" + id + path;
}

function attentionEntry(id) {
  let entry = state.attn.agents.get(id);
  if (!entry) {
    entry = {id, state: "loading", error: "", rows: [], note: "",
             reports: false, needs: 0,
             // What this agent reported, kept raw for the desktop status
             // panel: the requests themselves and one checkpoint per room.
             approvals: [], checkpoints: new Map(),
             // settled once this agent has answered at all: after that a
             // refresh is quiet and never takes the rows back down.
             settled: false, refreshing: false};
    state.attn.agents.set(id, entry);
  }
  return entry;
}

/* A checkpoint age the server already computed, said the way a person reads
   it. Nothing is derived from a clock here. */
function reportedAge(days) {
  if (days === null || days === undefined) return "";
  if (days < 1) return "today";
  const whole = Math.round(days);
  return whole === 1 ? "1 day ago" : whole + " days ago";
}

function approvalStep(approval) {
  const kind = String(approval.kind || "");
  if (kind.startsWith("command")) return "Approve a command before it runs";
  if (kind.startsWith("file_change")) return "Approve a file change before it is written";
  if (kind === "permissions") return "Grant a permission for this one request";
  if (kind === "user_input") {
    const first = ((approval.params || {}).questions || [])[0];
    return (first && first.title) || "Answer the runtime's question";
  }
  return "Answer the runtime's request";
}

/* What was reported, said exactly enough that the same report recognises
   itself and a different one does not. It is an identity, never a label: it
   is used to key a local dismissal and is never drawn. An approval's own key
   is what identifies it — not when it was polled, because a poll re-times the
   same request. A checkpoint is identified by when it was written and what it
   asked for. */
function attentionMark(agent, roomId, parts) {
  return [agent.id, String(roomId || "")]
    .concat(parts.map((part) => String(part === undefined || part === null ? "" : part)))
    .join("\0");
}

function attentionRow(agent, room, extra) {
  return Object.assign({
    key: agent.id + " " + String(room.id || ""),
    agent: agent.id,
    agentLabel: agent.label || agent.id,
    room: String(room.id || ""),
    project: room.project_name || room.project_id || "",
    title: room.title || room.session || room.id || "",
    // What this row is called on screen. A tab the person renamed wins.
    name: "",
    rank: 0,
    tone: "",
    condition: "",
    icon: "i-clock",
    step: "",
    next: "",
    age: "",
    from: "",
    fromTitle: "",
    // Empty means nothing here can be dismissed: there is no exact report to
    // key a dismissal to.
    fingerprint: "",
  }, extra || {});
}

/* Everything one agent reported, deduplicated to one row per conversation with
   the strongest condition that agent actually gave it. */
function attentionRowsFor(agent, attention, workspace) {
  const byKey = new Map();
  const keep = (row) => {
    const held = byKey.get(row.key);
    if (!held || row.rank > held.rank) byKey.set(row.key, row);
  };
  // Names come from this agent's own workspace, never from whichever agent is
  // selected: two agents can hold the same room id and mean different work.
  const listed = new Map();
  for (const project of (workspace && workspace.projects) || []) {
    for (const room of (project.pinned || []).concat(project.suggested || [])) {
      listed.set(String(room.id || ""), room);
    }
  }

  for (const approval of (attention && attention.approvals) || []) {
    const roomId = approval.room || "";
    if (!roomId) continue;                       // an unmapped request has nowhere to go
    const known = listed.get(roomId) || {};
    keep(attentionRow(agent, {id: roomId,
                              project_name: known.project_name || roomId.split("/")[0],
                              title: known.title || roomId.split("/")[1]}, {
      rank: 4, tone: "need", icon: "i-bell",
      condition: "Needs you",
      step: approvalStep(approval),
      age: approval.created_at ? when(approval.created_at) : "",
      from: "the runtime is blocked on this now",
      fingerprint: attentionMark(agent, roomId, [
        "approval", approval.key || approval.id, approval.kind,
        approval.thread_id, approval.turn_id]),
    }));
  }

  for (const room of (attention && attention.needs_person) || []) {
    const cp = room.checkpoint || {};
    keep(attentionRow(agent, room, {
      rank: 3, tone: "report", icon: "i-alert",
      condition: "Reported: needs " + (cp.need || "a person"),
      step: room.title || room.session,
      next: cp.next ? "Next: " + cp.next : "",
      age: reportedAge(room.reported_age_days),
      // Short enough to read at a glance; the timestamp and the caveat are on
      // the row itself for anyone who wants them.
      from: "reported by " + (cp.reporter || "someone") + " · not a live check",
      fromTitle: "reported " + (cp.state || "a state") + " / need " + (cp.need || "a person")
        + " by " + (cp.reporter || "someone") + (cp.at ? " at " + cp.at : "")
        + " — what was said at that time, not a check of the runtime now",
      fingerprint: attentionMark(agent, room.id, [
        "checkpoint", cp.at, cp.state, cp.need, cp.next]),
    }));
  }

  for (const room of (attention && attention.watching) || []) {
    const cp = room.checkpoint || {};
    keep(attentionRow(agent, room, {
      rank: 2, tone: "", icon: "i-clock",
      condition: "Waiting on " + (cp.need || "something external"),
      step: room.title || room.session,
      next: cp.next ? "Next: " + cp.next : "",
      age: cp.at || "",
      from: "no human action was requested",
    }));
  }

  for (const project of (workspace && workspace.projects) || []) {
    for (const room of (project.pinned || []).concat(project.suggested || [])) {
      keep(attentionRow(agent, room, {
        rank: room.reason === "open" ? 1 : 0,
        tone: "", icon: "i-clock",
        condition: room.reason_label || "",
        step: room.title || room.session,
        age: room.last_active
          ? room.last_active + (room.recency_source === "record" ? " (as recorded)" : "")
          : "",
      }));
    }
  }

  // The one live thing this browser can prove: the conversation on screen has
  // a turn the runtime itself reported. Reachability is never that proof.
  const detail = state.detail;
  if (detail && agent.id === agentId()) {
    const native = detail.native || {};
    if (native.active_turn || native.active_run === true) {
      const held = byKey.get(agent.id + " " + detail.id);
      if (held && held.rank < NEEDS_RANK) {
        held.tone = "work";
        held.condition = "Working";
        held.rank = 2.5;
      }
    }
  }
  return [...byKey.values()];
}

/* What this agent reported, kept as it arrived so the desktop status panel
   can use the request itself rather than a row built out of it. The requests
   are per room; so is the checkpoint, and a checkpoint that asked for a person
   outranks one that only watched. Staleness is the server's own judgement —
   it publishes both the age and the threshold — and is never a clock read
   here. */
function recordReports(entry, attention) {
  const after = Number((attention && attention.stale_after_days) || 0);
  entry.approvals = ((attention && attention.approvals) || []).filter((a) => a && a.room);
  const found = new Map();
  const note = (room, asks) => {
    const cp = (room && room.checkpoint) || {};
    if (!cp.at && !cp.need && !cp.state) return;
    const id = String((room && room.id) || "");
    const held = found.get(id);
    if (held && held.asks && !asks) return;
    const days = room.reported_age_days;
    found.set(id, {
      at: cp.at || "", state: cp.state || "", need: cp.need || "",
      next: cp.next || "", reporter: cp.reporter || "", asks,
      age: reportedAge(days),
      stale: Boolean(after && days !== null && days !== undefined && days > after),
    });
  };
  for (const room of (attention && attention.needs_person) || []) note(room, true);
  for (const room of (attention && attention.watching) || []) note(room, false);
  entry.checkpoints = found;
}

/* One agent's read. The first one shows that it is reading, because there is
   nothing else to show. Every read after that is quiet: the rows, the count
   and the summary already on screen stay exactly as they are until this
   request has something different to say. A register that empties itself every
   twenty seconds and fills back in is not reporting a change — it is only
   reporting that it asked again. */
async function loadAttentionFor(agent, gen) {
  const entry = attentionEntry(agent.id);
  const first = !entry.settled;
  entry.refreshing = true;
  if (first) { entry.state = "loading"; renderAttentionPanel(); }
  let attention = null;
  let workspace = null;
  let failure = "";
  // Both reads, but one failing must not blank the other.
  const results = await Promise.allSettled([
    api(agentPath(agent.id, "/api/attention"), {absolute: true}),
    api(agentPath(agent.id, "/api/workspace"), {absolute: true}),
  ]);
  // A newer pass owns this entry now, and has already flagged its own request.
  if (gen !== attentionGen) return;
  entry.refreshing = false;
  if (results[0].status === "fulfilled") attention = results[0].value;
  else failure = (results[0].reason && results[0].reason.message) || "attention unreadable";
  if (results[1].status === "fulfilled") workspace = results[1].value;
  else if (!failure) failure = (results[1].reason && results[1].reason.message) || "workspace unreadable";

  if (!attention && !workspace) {
    // A read that failed is a real change of state, not a flicker: rows that
    // can no longer be confirmed are taken down rather than left looking fresh.
    entry.state = "unavailable";
    entry.error = failure || "not reachable";
    entry.rows = [];
    entry.reports = false;
    entry.needs = 0;
    entry.settled = true;
    renderAttentionPanel();
    return;
  }
  // An adapter that reads no checkpoint says so, and is never counted as
  // having reported nothing to worry about.
  entry.reports = Boolean(attention && Array.isArray(attention.needs_person));
  entry.note = (attention && !entry.reports && attention.note) || "";
  entry.rows = attentionRowsFor(agent, attention, workspace);
  recordReports(entry, attention);
  // Count the rows on screen, not the raw obligations: two claims on one
  // conversation are one thing to go and look at, and a number that does not
  // match the list is a number nobody can act on.
  entry.needs = entry.reports
    ? entry.rows.filter((row) => row.rank >= NEEDS_RANK).length : 0;
  entry.state = "ready";
  entry.error = failure ? "partly read: " + failure : "";
  entry.settled = true;
  renderAttentionPanel();
}

/* Every agent is read at once and drawn as it lands, so one slow or dead host
   cannot hold up the rest, and none of them is blanked while it waits. */
function loadAttention() {
  const gen = ++attentionGen;
  const agents = attentionAgents();
  const wanted = new Set(agents.map((a) => a.id));
  for (const id of [...state.attn.agents.keys()]) {
    if (!wanted.has(id)) state.attn.agents.delete(id);
  }
  for (const agent of agents) { void loadAttentionFor(agent, gen); }
  // And the live state of every session open on this desktop, one read-only
  // room read each, bounded to one per refresh window.
  loadRoomStatuses(gen);
  renderAttentionPanel();
}

function scheduleAttentionRefresh() {
  clearTimeout(attentionTimer);
  if (state.ui.dock !== "attention") return;
  attentionTimer = setTimeout(() => {
    if (state.ui.dock !== "attention") return;
    loadAttention();
    scheduleAttentionRefresh();
  }, ATTENTION_REFRESH_MS);
}

/* Project order is the default and never moves on its own. Urgency is a
   deliberate choice. Both are total orders, so nothing re-sorts under a
   pointer between refreshes. */
function attentionSorted(rows) {
  const byProject = (a, b) =>
    a.agentLabel.localeCompare(b.agentLabel)
    || a.project.localeCompare(b.project)
    || b.rank - a.rank
    || a.title.localeCompare(b.title)
    || a.key.localeCompare(b.key);
  const byUrgency = (a, b) =>
    b.rank - a.rank
    || a.agentLabel.localeCompare(b.agentLabel)
    || a.project.localeCompare(b.project)
    || a.title.localeCompare(b.title)
    || a.key.localeCompare(b.key);
  return rows.slice().sort(state.attn.order === "urgency" ? byUrgency : byProject);
}

/* ------------------------------------------- the status of this desktop
   This panel is the state of every conversation open on this desktop, in the
   order the strip has them, and nothing else. Work that is not open here —
   an old project, a checkpoint somebody wrote three days ago — belongs in the
   command center, which is one button away at the bottom of the panel. A
   register that lists work you are not doing is a register you learn to
   ignore.

   Each row's state is read live, from that tab's own agent, over exactly one
   read-only route: GET /api/room/<room>, addressed through the tab's agent
   prefix. The conversation already on screen is never asked for twice — the
   console is already reading it — and no room is re-read while the last
   answer is still inside the refresh window. Nothing here attaches, continues,
   sends, releases or starts anything; there is no transcript scan and no model
   call.

   What the row is allowed to say is fixed, and each phrase means one thing:

     Running     the runtime reports a turn executing right now
     Needs you   a request is waiting — the row says whether it was observed
                 from the runtime or reported by somebody
     Blocked     the runtime reported a failure it did not come back from
     Paused      connected, with no turn running; whether that was deliberate
                 is for a person to decide, not for this panel to guess
     Unknown     nobody could see: the read failed, the session is held in
                 another terminal, or it is not connected here

   Nothing infers activity from anything else. A saved goal is a plan, not a
   pulse; a checkpoint is a note somebody wrote; an ownership record and a
   reachable host say nothing about whether a turn is running. A checkpoint
   may add the next step and who reported it, and may never overrule a live
   read. When a read fails the row says Unknown and carries the last state as
   an explanation — leaving a green Running over a dead read would be the one
   claim this panel must never make. */

const STATUS_FRESH_MS = ATTENTION_REFRESH_MS;   // one read per refresh, no more

const ATTN_DISMISS_KEY = "atlas.attn.dismissed";
const ATTN_DISMISS_MAX = 200;

function readDismissals() {
  const marks = new Map();
  let stored = null;
  try { stored = JSON.parse(window.localStorage.getItem(ATTN_DISMISS_KEY) || "null"); }
  catch (e) { stored = null; }
  const raw = stored && stored.marks;
  if (raw && typeof raw === "object") {
    for (const [mark, at] of Object.entries(raw)) {
      if (mark) marks.set(mark, Number(at) || 0);
    }
  }
  return marks;
}

function writeDismissals(marks) {
  // The oldest notes fall off first. A browser that never forgets a
  // fingerprint eventually hides something it should not.
  const kept = [...marks.entries()].sort((a, b) => b[1] - a[1]).slice(0, ATTN_DISMISS_MAX);
  marks.clear();
  for (const [mark, at] of kept) marks.set(mark, at);
  try {
    window.localStorage.setItem(ATTN_DISMISS_KEY,
      JSON.stringify({version: 1, marks: Object.fromEntries(kept)}));
  } catch (e) { /* private mode: dismissals last only this session */ }
}

/* The panel's own local state: what this browser has been told to hide, and
   whether the dismissed fold is open. No server is asked about it and none is
   told about it. */
function attnLocal() {
  if (!state.attn.local) {
    state.attn.local = {marks: readDismissals(), open: {dismissed: false}};
  }
  return state.attn.local;
}

function isAttentionDismissed(row) {
  return Boolean(row && row.fingerprint) && attnLocal().marks.has(row.fingerprint);
}

/* Hiding and unhiding are the same one local write, and neither one talks to
   an agent. Focus goes back to the row itself, because the control the person
   just used is the control that goes away. */
function markAttention(row, hidden) {
  if (!row || !row.fingerprint) return false;
  const local = attnLocal();
  if (hidden) local.marks.set(row.fingerprint, Date.now());
  else local.marks.delete(row.fingerprint);
  writeDismissals(local.marks);
  renderAttentionPanel();
  const list = $("#attnList");
  const back = list && list.querySelector('.attn-card[data-agent="' + CSS.escape(row.agent)
                                          + '"][data-room="' + CSS.escape(row.room) + '"]');
  if (back) back.focus();
  return true;
}

function dismissAttentionNotice(row) { return markAttention(row, true); }
function restoreAttentionNotice(row) { return markAttention(row, false); }

function toggleAttentionFold(id) {
  const open = attnLocal().open;
  open[id] = !open[id];
  renderAttentionPanel();
}

/* The strip exactly as it is drawn — one entry per native conversation, in
   the order the person put them in. Read here rather than restructured there:
   the panel follows the tabs, the tabs do not follow the panel. */
function attentionTabs() {
  const seen = new Set();
  const tabs = [];
  for (const tab of state.tabs || []) {
    const key = tabIdentity(tab);
    if (seen.has(key)) continue;
    seen.add(key);
    tabs.push(tab);
  }
  return tabs;
}

/* ------------------------------------------------------ the live room read */
function roomStatusCache() {
  if (!state.attn.rooms) state.attn.rooms = new Map();
  return state.attn.rooms;
}

function roomStatusEntry(tab) {
  const cache = roomStatusCache();
  const key = tabKey(tab.agent, tab.room);
  let entry = cache.get(key);
  if (!entry) {
    entry = {key, agent: tab.agent, room: tab.room,
             detail: null, at: 0, error: "", reading: false};
    cache.set(key, entry);
  }
  return entry;
}

/* What is known about this tab's room right now. The conversation on screen
   is answered from what the console is already holding — it is the freshest
   thing in the browser, and asking for it again would be a second answer to a
   question already answered. */
function roomStatusOf(tab) {
  if (tab.agent === agentId() && tab.room === state.room && state.detail) {
    // The console holds this one — but only while it is still connected. A
    // dropped connection makes what it holds a memory, not an observation.
    const error = state.connKind === "off" ? "the connection to this console dropped" : viewFreshnessError();
    return {detail: state.detail, current: !error, reading: state.roomRefreshing, error};
  }
  const entry = roomStatusCache().get(tabKey(tab.agent, tab.room));
  if (!entry) return null;
  return {detail: entry.detail, error: entry.error, reading: entry.reading,
          current: Boolean(entry.detail) && !entry.error};
}

/* One tab's read. Read-only, bounded to one per refresh window, and never
   issued for a room this browser is already reading continuously. */
async function loadRoomStatus(tab, gen) {
  const entry = roomStatusEntry(tab);
  // Nowhere to ask: this console does not serve that agent.
  if (tab.unavailable) {
    entry.error = tab.unavailable;
    return;
  }
  // The open conversation. Keep a copy so leaving it does not blank the row,
  // but never spend a request on it.
  if (tab.agent === agentId() && tab.room === state.room && state.detail) {
    entry.detail = state.detail;
    entry.at = state.freshness.room;
    entry.error = viewFreshnessError();
    return;
  }
  if (entry.reading) return;
  if (entry.at && Date.now() - entry.at < STATUS_FRESH_MS) return;
  entry.reading = true;
  try {
    const detail = await api(agentPath(tab.agent, "/api/room/" + encodeURI(tab.room)),
                             {absolute: true});
    if (gen !== attentionGen) { entry.reading = false; return; }
    entry.detail = detail;
    entry.at = Date.now();
    entry.error = "";
  } catch (error) {
    if (gen !== attentionGen) { entry.reading = false; return; }
    // Keep the last state on screen and stop calling it current. A row that
    // empties itself because one read failed is reporting the read, not the
    // work.
    entry.error = (error && error.message) || "not reachable";
  }
  entry.reading = false;
  renderAttentionPanel();
}

/* Every open tab is read at once and drawn as it lands, so one slow or dead
   host cannot hold up the rest and none of them is blanked while it waits. */
function loadRoomStatuses(gen) {
  const wanted = new Set();
  for (const tab of attentionTabs()) {
    wanted.add(tabKey(tab.agent, tab.room));
    void loadRoomStatus(tab, gen);
  }
  for (const key of [...roomStatusCache().keys()]) {
    if (!wanted.has(key)) roomStatusCache().delete(key);
  }
}

/* ---------------------------------------------------------- reading a state */
/* The requests waiting on one room. A room detail that answered carries the
   whole truth about that room, including "none": a request that has since
   been answered is simply gone from it, and an empty list is an answer, not a
   missing one. The agent-wide list is a fallback only for a runtime that does
   not publish the key at all — otherwise a resolved request could be unioned
   back in from a cached poll and go on asking for a person who already dealt
   with it. */
function approvalsFor(tab, detail) {
  if (detail && Array.isArray(detail.approvals)) return detail.approvals;
  const entry = state.attn.agents.get(tab.agent);
  return ((entry && entry.approvals) || []).filter((a) => a && a.room === tab.room);
}

/* The checkpoint this agent reported for one room, and whether it asked for a
   person at all. A checkpoint the server itself calls stale is kept as
   context and never as a claim about now. */
function checkpointFor(tab) {
  const entry = state.attn.agents.get(tab.agent);
  return (entry && entry.checkpoints && entry.checkpoints.get(tab.room)) || null;
}

/* Goal evidence is independent of turn activity. Older adapters that omit
   the contract cannot confirm that a session has no goal. */
function goalStatus(detail) {
  const status = detail?.goal_status;
  if (status?.state === "unsupported") return {state: "unsupported", goal: null};
  if (status?.state === "known" && (status.goal === null
      || (status.goal && typeof status.goal === "object" && !Array.isArray(status.goal)))) return status;
  return {state: "unknown", goal: null};
}
function goalLabel(status) {
  if (status.state === "unsupported") return "Goal unavailable";
  if (status.state !== "known") return "Goal";
  if (!status.goal) return "No goal";
  const labels = {active: "active", paused: "paused", blocked: "blocked",
    usageLimited: "usage limited", complete: "complete"};
  return labels[status.goal.status] ? "Goal " + labels[status.goal.status] : "Goal";
}
function goalTone(status) {
  if (status.state !== "known") return "neutral";
  return {active: "active", paused: "paused", blocked: "blocked", usageLimited: "blocked"}[status.goal?.status] || "neutral";
}
function goalDescription(status) {
  return status.goal?.objective || status.goal?.text || (status.state === "unknown"
    ? "Goal status unknown" : goalLabel(status));
}
function renderGoalBadge() {
  const button = $("#btnGoal"), status = goalStatus(state.detail);
  button.hidden = !state.detail || status.state === "unsupported";
  button.textContent = goalLabel(status);
  button.dataset.goalTone = goalTone(status);
  button.title = goalDescription(status);
  button.setAttribute("aria-label", goalLabel(status) + ": " + goalDescription(status) + ". View goal details");
}

/* Inspection never uses sendCommand or openSession: it must not attach a
   worker, touch the composer, or send a turn, including from Attention. */
let goalMenuAnchor = null;
async function openGoalPanel(agent = agentId(), room = state.room, detail = state.detail, anchor = $("#btnGoal")) {
  goalMenuAnchor = anchor;
  dismissOverlay();
  const target = {agent, room};
  state.goalPanelTarget = target;
  state.goalPanelStatus = goalStatus(detail);
  state.goalPanel = state.goalPanelStatus.goal;
  showCommands("goal", false);
  $("#commandClose").focus();
  if (state.goalPanelStatus.state === "unsupported") return;
  try {
    const result = await api(agentPath(agent, "/api/room/" + encodeURI(room) + "/command"),
      {absolute: true, method: "POST", body: {command: "/goal", client_id: clientId()}});
    if (state.goalPanelTarget !== target || state.commandMode !== "goal") return;
    const outcome = result.command || {}, native = outcome.native || {};
    if (outcome.state === "unsupported") state.goalPanelStatus = {state: "unsupported", goal: null};
    else if (["failed", "uncertain", "needs_input", "needs_sign_in"].includes(outcome.state))
      state.goalPanelStatus = {state: "unknown", goal: state.goalPanel, message: outcome.message};
    else if (Object.hasOwn(outcome, "goal") || Object.hasOwn(native, "goal"))
      state.goalPanelStatus = {state: "known", goal: Object.hasOwn(outcome, "goal") ? outcome.goal : native.goal};
    else state.goalPanelStatus = {state: "unknown", goal: state.goalPanel};
    state.goalPanel = state.goalPanelStatus.goal;
  } catch (error) {
    if (state.goalPanelTarget !== target || state.commandMode !== "goal") return;
    state.goalPanelStatus = {state: "unknown", goal: state.goalPanel, message: error.message};
  }
  if (state.goalPanelTarget === target && state.commandMode === "goal") renderCommands();
}

const STATUS_TONE = {
  need: {tone: "need", icon: "i-bell"},
  reported: {tone: "need", icon: "i-bell"},
  blocked: {tone: "stop", icon: "i-alert"},
  running: {tone: "work", icon: "i-clock"},
  paused: {tone: "", icon: "i-clock"},
  unknown: {tone: "mute", icon: "i-clock"},
};

/* One open tab, read into one row.

   Four things can be said about a session, and each is said only when it was
   observed. Running means the runtime reports a turn executing right now.
   Needs you means a request is waiting — from the runtime itself, or reported
   by somebody, and the row says which. Paused means the session is connected
   and nothing is running; whether that was on purpose is for a person to
   decide, not for this panel to guess. Unknown means nobody could see: the
   read failed, the session is held in another terminal, or it is not
   connected here. Blocked is kept for a failure the runtime actually
   reported, because that is worth its own colour.

   Nothing infers activity from anything else. A saved goal, a checkpoint, an
   ownership record and a reachable host are none of them a running turn. */
function tabStatusRow(tab) {
  const agent = {id: tab.agent, label: agentLabel(tab.agent)};
  const status = roomStatusOf(tab);
  const detail = status && status.detail;
  const stale = Boolean(status && status.detail && status.error);
  const cp = checkpointFor(tab);
  const approvals = approvalsFor(tab, detail);
  const native = (detail && detail.native) || {};
  const ownership = (detail && detail.ownership) || {};
  const goal = stale ? {state: "unknown", goal: null} : goalStatus(detail);

  let kind = "unknown";
  let condition = "Unknown";
  let step = "";
  let from = "";
  let rank = 0;
  let fingerprint = "";

  if (approvals.length) {
    const first = approvals[0];
    kind = "need"; rank = 4;
    condition = approvals.length > 1 ? approvals.length + " requests waiting" : "Needs you";
    step = approvalStep(first);
    from = "waiting for you now";
    fingerprint = attentionMark(agent, tab.room, [
      "approval", first.key || first.id, first.kind, first.thread_id, first.turn_id]);
  } else if ((native.active_turn && native.active_turn !== detail?.native_terminal?.turn_id) || native.active_run === true) {
    kind = "running"; rank = 2;
    condition = "Running";
    step = "Executing a turn";
    if (ownership.state === "held_elsewhere") from = "open in another terminal";
  } else if (detail && detail.native_terminal && terminalSummary(detail)) {
    const terminal = detail.native_terminal;
    const currentBlock = ["limited", "sign_in_required"].includes(accountState(detail));
    kind = currentBlock ? "blocked" : "unknown"; rank = currentBlock ? 3.5 : 0;
    condition = currentBlock ? "Blocked" : "Previous attempt";
    step = terminalSummary(detail);
    from = currentBlock ? "current account status" : "historical report · current availability unknown";
    fingerprint = attentionMark(agent, tab.room, [
      "terminal", terminal.kind, terminal.status, terminal.message]);
  } else if (cp && cp.asks && !cp.stale) {
    // Somebody asked for a person here. That is worth the same colour as a
    // runtime request, and it says out loud that it is a report, not a check.
    kind = "reported"; rank = 3;
    condition = "Needs you · reported";
    step = cp.next || ("Needs " + (cp.need || "a person"));
    from = "reported by " + (cp.reporter || "someone")
      + (cp.age ? " " + cp.age : "") + " · not a live check";
    fingerprint = attentionMark(agent, tab.room, [
      "checkpoint", cp.at, cp.state, cp.need, cp.next]);
  } else if (!detail) {
    kind = "unknown"; rank = 0;
    condition = tab.unavailable ? "Offline" : "Unknown";
    step = tab.unavailable || (status && status.error) || "Not read yet";
  } else if (ownership.state === "held_elsewhere") {
    // Held somewhere else is not proof of anything. It is not paused and it is
    // not running; it is out of sight.
    kind = "unknown"; rank = 0;
    condition = "Unknown";
    step = "Open in another terminal · no turn reported";
  } else if (detail.controllable && ownership.state === "atlas_owned") {
    kind = "paused"; rank = 1;
    condition = "Paused";
    step = "No turn running";
  } else if (ownership.state === "idle") {
    kind = "unknown"; rank = 0;
    condition = "Unknown";
    step = "Not connected here · nothing to observe";
  } else {
    kind = "unknown"; rank = 0;
    condition = "Unknown";
    step = detail.capability_short || "Could not check who holds this";
  }

  // A read that failed is a read that failed. Whatever it last said, it is no
  // longer being observed — and a green Running left on screen over a dead
  // read is the exact claim this panel must not make. The last state is kept
  // as an explanation, never as the headline.
  if (stale) {
    const last = condition;
    kind = "unknown"; rank = 0;
    condition = "Unknown";
    step = "Last read failed · last seen " + last;
    from = (status && status.error) || "the session could not be read";
  }

  // A checkpoint that asked for a person is context under a live state: it
  // adds the next step and says it was reported rather than checked. It never
  // changes what the row says is happening. A checkpoint that only watched
  // adds nothing, because it asked for nobody.
  let next = "";
  let fromTitle = "";
  if (cp && cp.asks) {
    fromTitle = "reported " + (cp.state || "a state") + " / need " + (cp.need || "a person")
      + " by " + (cp.reporter || "someone") + (cp.at ? " at " + cp.at : "")
      + " — what was said at that time, not a check of the runtime now";
    if (kind !== "reported" && !stale) {
      if (cp.next) next = "Next: " + cp.next;
      if (!from) {
        from = "a checkpoint " + (cp.age || "on record") + " asked for a person · not a live check";
      }
    }
  }

  const tone = STATUS_TONE[kind] || STATUS_TONE.unknown;
  return attentionRow(agent, {id: tab.room, project_name: tab.project,
                              title: canonicalLabel(tab)}, {
    rank, kind, condition, step, next, from, fromTitle, fingerprint, goal,
    tone: tone.tone, icon: tone.icon,
    name: tabLabel(tab),
    stale,
    aside: "",
    age: "",
  });
}

async function openAttentionRow(row) {
  dismissOverlay();
  await openSession(row.agent, row.room, {toTail: true, connect: true});
}

/* Tab navigation and detail disclosure are separate controls. */
const attentionExpanded = new Map();
function attentionCard(row, showAgent) {
  const name = row.name || row.title || row.room;
  const blocked = ["blocked", "need", "reported"].includes(row.kind) || ["blocked", "usageLimited"].includes(row.goal?.goal?.status);
  const signal = [row.kind, row.fingerprint || "", row.goal?.goal?.status || ""].join("|");
  const previous = attentionExpanded.get(row.key);
  const open = previous?.signal === signal ? previous.open : blocked && !row.dismissed;
  const selected = row.agent === agentId() && row.room === state.room;
  const item = el("div", {class: "attn-item attn-compact" + (selected ? " selected" : ""), data: {key: row.key}});
  const main = el("button", {class: "attn-jump", type: "button", title: name + " · " + row.condition,
    "aria-label": "Open " + name + " · " + row.condition,
    data: {agent: row.agent, room: row.room, kind: row.kind}, on: {click: () => openAttentionRow(row)}}, [
    el("span", {class: "status-light status-" + row.kind, "aria-hidden": "true"}),
    el("span", {class: "attn-project", text: name}),
    el("span", {class: "attn-short", text: row.kind === "paused" ? "Idle" : row.condition.replace(" · reported", "*")}),
  ]);
  const goal = row.goal || {state: "unknown", goal: null};
  const goalText = goal.state === "known" ? (goal.goal ? "◎" : "○") : "?";
  const goalName = goal.state === "unknown" ? "Goal unknown" : goalLabel(goal);
  const detail = el("div", {class: "attn-compact-detail", id: "attn-detail-" + encodeURIComponent(row.key), hidden: !open});
  const toggle = el("button", {class: "attn-expand", type: "button", text: open ? "⌃" : "⌄",
    "aria-label": "Details for " + name, "aria-expanded": String(open), "aria-controls": detail.id,
    on: {click: () => { const next = detail.hidden; detail.hidden = !next;
      attentionExpanded.set(row.key, {open: next, signal}); toggle.textContent = next ? "⌃" : "⌄";
      if (attentionExpanded.size > 200) attentionExpanded.delete(attentionExpanded.keys().next().value);
      toggle.setAttribute("aria-expanded", String(next)); }}});
  item.appendChild(el("div", {class: "attn-compact-line"}, [main,
    el("span", {class: "attn-goal-light " + goalTone(goal), text: goalText,
      role: "img", "aria-label": goalName, title: goalName + " · " + goalDescription(goal)}), toggle]));
  detail.appendChild(el("p", {class: "attn-detail-location", text: [row.agentLabel, row.project].filter(Boolean).join(" · ")}));
  if (row.step) detail.appendChild(el("p", {class: "attn-step", text: row.step}));
  if (row.aside) detail.appendChild(el("p", {class: "attn-next", text: row.aside}));
  if (row.next) detail.appendChild(el("p", {class: "attn-next", text: row.next}));
  if (row.from) detail.appendChild(el("p", {class: "attn-from", text: row.from, title: row.fromTitle || ""}));
  detail.appendChild(el("p", {class: "attn-from", text: goalName + (goal.goal ? ": " + goalDescription(goal) : "")}));
  if (row.stale) detail.appendChild(el("p", {class: "attn-stale", text: "Last read failed · status is not current"}));
  if (row.goal?.goal) detail.appendChild(el("button", {class: "ws-button", type: "button", text: "Goal details",
    on: {click: event => openGoalPanel(row.agent, row.room, {goal_status: row.goal}, event.currentTarget)}}));
  if (row.dismissed) detail.appendChild(el("p", {class: "attn-hushed", text: "Dismissed here · nothing was answered or stopped"}));
  else if (row.fingerprint && row.rank >= NEEDS_RANK) detail.appendChild(el("button", {class: "ws-button", type: "button",
    text: "Dismiss notice", "aria-label": "Dismiss this notice for " + name, on: {click: () => dismissAttentionNotice(row)}}));
  item.appendChild(detail);
  return item;
}

/* One line in the dismissed fold: what was hidden, and the way back. */
function attentionRestore(row) {
  const name = row.name || row.title || row.room;
  return el("div", {class: "attn-back", data: {key: row.key}}, [
    el("span", {class: "attn-back-name",
      text: [row.agentLabel, name].filter(Boolean).join(" · ")}),
    el("button", {
      class: "attn-restore", type: "button",
      "aria-label": "Restore this notice for " + name,
      data: {agent: row.agent, room: row.room},
      on: {click: () => restoreAttentionNotice(row)},
    }, [el("span", {text: "Restore"})]),
  ]);
}

/* Exactly what the panel would draw, as plain data, before anything is built.
   Each block carries a stable key and its own signature: the key decides
   whether it is the same block, the signature whether it needs redrawing. */
function attentionView() {
  const agents = attentionAgents();
  const showAgent = agents.length > 1;
  const local = attnLocal();
  const rows = [];
  for (const tab of attentionTabs()) {
    const row = tabStatusRow(tab);
    row.dismissed = isAttentionDismissed(row);
    rows.push(row);
  }

  // The count is the rows on this list that ask for a person and have not
  // been dismissed. Nothing outside this desktop can move it.
  const needs = rows.filter((r) => r.rank >= NEEDS_RANK && !r.dismissed).length;
  const hushed = rows.filter((r) => r.dismissed);

  const summary = !rows.length ? "Nothing is open on this desktop"
    : "On this desktop · " + (needs ? needs + (needs === 1 ? " needs you" : " need you")
                                    : "nothing needs you");

  const blocks = [];
  const push = (block) => { blocks.push(block); return block; };

  for (const row of rows) {
    push({kind: "card", key: "row:" + row.key, row, showAgent,
          sig: [row.key, row.agent === agentId() && row.room === state.room, showAgent ? row.agentLabel : "", row.project, row.name,
                row.condition, row.tone, row.icon, row.step, row.aside, row.next,
                row.from, row.fromTitle, row.stale, row.dismissed,
                row.goal?.state, row.goal?.goal?.status, goalDescription(row.goal || {state: "unknown"}),
                // The report this row is keyed to. A card holds its own row in
                // the closure Dismiss acts on, so when the identity of what is
                // being reported changes the card has to be rebuilt with it —
                // otherwise Dismiss would file the notice under the report it
                // was drawn for, not the one it is showing.
                row.fingerprint, row.rank >= NEEDS_RANK]});
  }
  if (!rows.length) {
    push({kind: "empty", key: "empty",
          text: "Open a session and it will be listed here.", sig: ["none"]});
  }
  if (hushed.length) {
    push({kind: "fold", key: "fold:dismissed", id: "dismissed", label: "Dismissed",
          meta: hushed.length === 1 ? "1 notice" : hushed.length + " notices",
          open: Boolean(local.open.dismissed),
          sig: [hushed.length, Boolean(local.open.dismissed)]});
    if (local.open.dismissed) {
      for (const row of hushed) {
        push({kind: "restore", key: "back:" + row.key, row,
              sig: [row.key, row.name, row.fingerprint]});
      }
    }
  }
  return {needs, summary, blocks};
}

function attentionBlockNode(block) {
  if (block.kind === "card") return attentionCard(block.row, block.showAgent);
  if (block.kind === "restore") return attentionRestore(block.row);
  if (block.kind === "fold") {
    const node = el("button", {
      class: "attn-fold", type: "button", "aria-expanded": String(block.open),
      data: {fold: block.id, key: block.key},
    }, [
      useIcon("i-chev"),
      el("span", {class: "attn-fold-name", text: block.label}),
      el("span", {class: "attn-fold-meta", text: block.meta}),
    ]);
    node.addEventListener("click", () => toggleAttentionFold(block.id));
    return node;
  }
  return el("p", {class: "attn-empty", text: block.text});
}

/* What was drawn last time, so the next draw can touch only what moved. */
let attentionDrawn = {head: "", keys: [], sigs: []};

/* Put the focus back where it was after one node was replaced under it. The
   person did not move; only the row's contents did. */
function refocusWithin(before, after) {
  const active = document.activeElement;
  if (!active || !before.contains(active)) return;
  const wanted = String(active.className || "").split(" ")[0];
  if (!wanted) return;
  const back = after.classList.contains(wanted) ? after : after.querySelector("." + wanted);
  if (back && back.focus) back.focus();
}

function renderAttentionPanel(force) {
  const host = $("#attnList");
  if (!host) return;
  const view = attentionView();
  const keys = view.blocks.map((b) => b.key);
  const sigs = view.blocks.map((b) => JSON.stringify(b.sig));
  const head = JSON.stringify([view.needs, view.summary]);
  const drawn = attentionDrawn;
  const sameShape = !force
    && host.childElementCount === view.blocks.length
    && keys.length === drawn.keys.length
    && keys.every((key, i) => key === drawn.keys[i]);

  // Nothing a person can see has changed, so nothing is touched at all. This
  // is what keeps a focused card focused and a scrolled list where it was.
  if (sameShape && head === drawn.head && sigs.every((sig, i) => sig === drawn.sigs[i])) return;

  attentionDrawn = {head, keys, sigs};

  const summary = $("#attnSummary");
  if (summary) summary.textContent = view.summary;
  for (const node of [$("#attnCount"), $("#attnNavCount")]) {
    if (!node) continue;
    node.textContent = String(view.needs);
    node.hidden = view.needs === 0;
  }
  const bell = $("#btnAttention");
  if (bell) {
    bell.title = view.needs ? "Attention · " + view.needs + " needing you here" : "Attention";
  }
  // Ordering belongs to the strip now: this list is the strip, in its order.
  const sort = $("#attnSort");
  if (sort) sort.hidden = true;

  if (sameShape) {
    // One session changed state. Replace that one row and leave every other
    // node — and the focus ring inside them — exactly where it was.
    for (let i = 0; i < view.blocks.length; i += 1) {
      if (sigs[i] === drawn.sigs[i]) continue;
      const held = host.children[i];
      const next = attentionBlockNode(view.blocks[i]);
      host.replaceChild(next, held);
      refocusWithin(held, next);
    }
    return;
  }

  host.replaceChildren();
  for (const block of view.blocks) host.appendChild(attentionBlockNode(block));
}

/* The strip's order is the person's order and is the only order there is. */
function setAttentionOrder(order) {
  state.attn.order = order;
  const button = $("#attnSort");
  if (button) button.hidden = true;
  renderAttentionPanel();
}

/* ------------------------------------------------------------- views/shell */
function showView(which) {
  $("#app").dataset.workspaceView = ["email", "constellation", "schedule", "usage"].includes(which) ? which : "";
  $("#viewConsole").hidden = which !== "console";
  $("#viewBoard").hidden = which !== "board";
  $("#viewTell").hidden = which !== "tell";
  for (const [kind,name] of [["schedule","Schedule"],["usage","Usage"]]) {
    $("#view"+name).hidden = which !== kind;
    $("#btn"+name).setAttribute("aria-pressed", String(which === kind));
  }
  $("#viewEmail").hidden = which !== "email";
  $("#viewConstellation").hidden = which !== "constellation";
  $("#btnEmail").setAttribute("aria-pressed", String(which === "email"));
  $("#btnConstellation").setAttribute("aria-pressed", String(which === "constellation"));
  if (["email", "constellation", "schedule", "usage"].includes(which) && window.__workspace) window.__workspace.open(which);
  $("#btnBoard").setAttribute("aria-pressed", String(which === "board"));
  $("#btnConsole").setAttribute("aria-pressed", String(which === "console"));
  $("#btnTell").setAttribute("aria-pressed", String(which === "tell"));
  for (const id of ["#btnFold", "#btnEarlier", "#btnFocus", "#btnMore"]) {
    const node = $(id);
    if (node) node.hidden = which !== "console";
  }
  if (which === "board") renderBoard();
  if (which === "tell" && window.__tell) window.__tell.open();
}

/* Wide enough for a docked drawer beside a console that still reads well.
   Below this the drawers come over the console instead of squeezing it. */
const WIDE = "(min-width:1100px)";
function isWide() { return window.matchMedia(WIDE).matches; }

function savePref(key, value) {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch (e) { /* private mode: the shell simply starts at its default */ }
}
function readPref(key) {
  try { return window.localStorage.getItem(key) || ""; } catch (e) { return ""; }
}

const PANEL_TITLES = {attention: "Your workspace", turns: "My turns", updates: "Updates",
                      source: "Original text", board: "Canvas", notes: "Notes", details: "Room settings"};

/* One function owns every visible shell state, so a resize, a preference and
   a click can never disagree about what is open. */
function applyShell() {
  const app = $("#app");
  const wide = isWide();
  if (wide && state.ui.overlay) state.ui.overlay = null;
  if (state.ui.dock === "details") state.ui.dock = "notes";
  const dockKind = state.ui.dock;
  if (wide) {
    app.dataset.left = state.ui.left === "rail" ? "rail" : "wide";
    app.dataset.dock = dockKind ? "open" : "closed";
  } else {
    app.dataset.left = state.ui.overlay === "left" ? "overlay" : "off";
    app.dataset.dock = (state.ui.overlay === "dock" && dockKind) ? "overlay" : "closed";
  }
  // Focus puts the chrome away without changing what it was: the values above
  // are left alone, so leaving focus needs nothing restored.
  if (state.ui.focus) { app.dataset.left = "off"; app.dataset.dock = "closed"; }
  const dockShown = app.dataset.dock !== "closed";
  $("#dock").hidden = !dockShown;
  $("#scrim").hidden = !state.ui.overlay || Boolean(state.ui.focus);
  renderWorkToggle();
  $("#btnLeftToggle").setAttribute("aria-expanded", String(app.dataset.left === "wide"));
  $("#btnMenu").setAttribute("aria-expanded", String(app.dataset.left === "overlay"));
  $("#btnDock").setAttribute("aria-expanded", String(dockShown));
  $("#btnDock").title = dockShown ? "Close the supporting panel" : "Open the supporting panel";
  for (const [id, kind] of [["#btnMore", "details"]]) {
    const node = $(id);
    if (node) node.setAttribute("aria-expanded", String(dockShown && dockKind === kind));
  }
  for (const tab of document.querySelectorAll(".dock-tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.panel === dockKind));
  }
  // A drawer over the console is modal and says so; a docked one is not.
  for (const [node, overlay] of [[$("#side"), app.dataset.left === "overlay"],
                                 [$("#dock"), app.dataset.dock === "overlay"]]) {
    if (overlay) { node.setAttribute("role", "dialog"); node.setAttribute("aria-modal", "true"); }
    else { node.removeAttribute("role"); node.removeAttribute("aria-modal"); }
  }
  $("#dockTitle").textContent = PANEL_TITLES[dockKind] || "Supporting panel";
  $("#btnAttention").setAttribute("aria-expanded", String(dockShown && dockKind === "attention"));
  $("#panelAttention").hidden = dockKind !== "attention";
  $("#panelNav").hidden = !(dockKind === "turns" || dockKind === "updates");
  $("#panelSource").hidden = dockKind !== "source";
  $("#panelBoard").hidden = dockKind !== "board";
  $("#panelNotes").hidden = dockKind !== "notes";
}

function renderPanel(force) {
  const kind = state.ui.dock;
  if (kind === "attention") {
    // Reading every agent is a network round each; do it on open and on a
    // bounded timer, never on an ordinary redraw.
    renderAttentionPanel();
    if (force || !state.attn.agents.size) loadAttention();
    scheduleAttentionRefresh();
  } else if (kind === "turns" || kind === "updates") setupNav(kind, force);
  else if (kind === "source") renderSourcePanel();
  else if (kind === "board") { renderBoardPanel(); if (force || state.board.key !== (boardContext() || {}).key) void loadBoard(force); scheduleBoardPoll(); }
  else if (kind === "notes") { renderNotes(); if (force) void refreshNotes(); }
}

function openRoomSettings() {
  dismissOverlay();
  renderReadingControls(); renderDetails(); renderVoicePicker();
  const dialog = $("#roomSettingsDialog");
  if (!dialog.open) dialog.showModal();
  void loadExecutionPolicy();
  void loadServiceRecovery();
}
$("#roomSettingsClose").addEventListener("click", () => $("#roomSettingsDialog").close());

function openPanel(kind, trigger) {
  if (kind === "details") { openRoomSettings(); return; }
  const wide = isWide();
  state.ui.dock = kind;
  state.ui.overlay = wide ? null : "dock";
  if (trigger) state.lastTrigger = trigger;
  if (wide && kind !== "source") savePref("atlas.dock", kind);
  applyShell();
  renderPanel();
  // Opening the register is a deliberate ask for the current picture.
  if (kind === "attention") loadAttention();
  if (kind === "board") void loadBoard();
  if (kind === "notes") void refreshNotes();
  const first = kind === "turns" || kind === "updates" ? $("#navSearch")
    : kind === "details" ? $("#moreFold")
    : kind === "attention" ? $("#attnSort") : $("#dockClose");
  if (!wide || kind === "turns" || kind === "updates") setTimeout(() => first && first.focus(), 0);
  if (kind !== "source") publishCustomizations();
}

function closeDock(returnFocus) {
  const wasOverlay = state.ui.overlay === "dock";
  state.ui.dock = "";
  clearTimeout(attentionTimer);
  clearTimeout(state.board.poll);
  if (wasOverlay) state.ui.overlay = null;
  savePref("atlas.dock", "");
  applyShell();
  publishCustomizations();
  if (returnFocus && state.lastTrigger && document.contains(state.lastTrigger)) {
    state.lastTrigger.focus();
    state.lastTrigger = null;
  }
}

/* On a phone a drawer covers the console, so acting inside it should hand the
   screen back. On a desktop the drawer is docked beside the console and stays. */
function dismissOverlay() {
  if (!state.ui.overlay) return;
  if (state.ui.overlay === "dock") state.ui.dock = "";
  state.ui.overlay = null;
  applyShell();
}

function setLeft(mode) {
  state.ui.left = mode;
  savePref("atlas.left", mode);
  applyShell();
  publishCustomizations();
}

function toggleLeft(trigger) {
  if (isWide()) { setLeft(state.ui.left === "wide" ? "rail" : "wide"); return; }
  if (state.ui.overlay === "left") { closeOverlay(true); return; }
  state.ui.overlay = "left";
  if (trigger) state.lastTrigger = trigger;
  applyShell();
  setTimeout(() => $("#roomSearch").focus(), 0);
}

function closeOverlay(returnFocus) {
  if (!state.ui.overlay) return;
  if (state.ui.overlay === "dock") state.ui.dock = "";
  state.ui.overlay = null;
  applyShell();
  if (returnFocus && state.lastTrigger && document.contains(state.lastTrigger)) {
    state.lastTrigger.focus();
    state.lastTrigger = null;
  }
}

function overlayNode() {
  if (state.ui.overlay === "left") return $("#side");
  if (state.ui.overlay === "dock") return $("#dock");
  return null;
}

/* --------------------------------------------------------- source panel */
/* The exact text keeps the room it came from. After a switch it says so
   instead of quietly reading as if it belonged to the session now open. */
function roomLabel(detail) {
  if (!detail) return "";
  return (detail.project_name ? detail.project_name + " · " : "")
    + (detail.title || detail.session || detail.id || "");
}

function openSource(item, trigger) {
  state.source = {
    room: state.room,
    label: roomLabel(state.detail) || state.room || "an earlier room",
    item: item.id,
    turn: item.turn_id || "",
    phase: item.phase || "",
    text: item.text || "",
  };
  openPanel("source", trigger);
  redrawKeepingPlace();
}

function renderSourcePanel() {
  const src = state.source;
  const origin = $("#srcOrigin");
  const body = $("#srcText");
  const acts = $("#srcActs");
  acts.replaceChildren();
  if (!src) {
    origin.className = "panel-sub";
    origin.textContent = "Choose “Original text” in any agent reply’s menu to read the exact text the "
      + "runtime reported, character for character.";
    body.textContent = "";
    return;
  }
  const elsewhere = src.room !== state.room;
  origin.className = "origin" + (elsewhere ? " elsewhere" : "");
  const lines = [(elsewhere ? "From another room: " : "From ") + src.label,
                 "item " + shortId(src.item) + (src.turn ? " · turn " + shortId(src.turn) : "")
                   + (src.phase ? " · " + String(src.phase).replace("_", " ") : "")];
  if (elsewhere) {
    lines.push("You are now in " + (roomLabel(state.detail) || "another session")
      + ", so this text is not from the session on screen.");
  }
  origin.textContent = lines.join("\n");
  body.textContent = src.text;
  if (elsewhere) {
    acts.appendChild(el("button", {class: "linkbtn", type: "button", text: "Open that room",
      on: {click: () => selectRoom(src.room, {connect: true})}}));
  } else {
    acts.appendChild(el("button", {class: "linkbtn", type: "button", text: "Find in transcript",
      on: {click: () => { jumpToItem(src.item); dismissOverlay(); }}}));
  }
  acts.appendChild(el("button", {class: "linkbtn", type: "button", text: "Clear",
    on: {click: () => { state.source = null; renderSourcePanel(); renderStream(); }}}));
}

/* --------------------------------------------------------------- board */
/* A board belongs to one agent and one room context. It is workspace-owned
   metadata, never a command to the native process behind that conversation. */
const BOARD_POLL_MS = 15000;
const BOARD_STATES = ["info", "todo", "doing", "done", "blocked"];

function boardContext() {
  const detail = state.detail;
  if (!detail || !state.room) return null;
  const agent = agentId();
  const project = String(detail.project_id || "");
  const session = String(detail.session || state.room.split("/").pop() || "");
  if (!project || !session) return null;
  return {agent, room: state.room, project, session,
    key: agent + "\0" + state.room + "\0" + project + "\0" + session};
}
function boardPath(context) {
  return "/api/boards/" + encodeURIComponent(context.agent) + "/"
    + encodeURIComponent(context.project) + "/" + encodeURIComponent(context.session);
}
function cloneBoard(value) { return JSON.parse(JSON.stringify(value || {})); }
function blankBoard(context) {
  return {title: (context && context.project) || "Canvas", reporter: "human",
    sections: [{title: "Now", items: [{label: "What needs attention?", state: "todo"}]}]};
}
function cleanCanvasChart(value) {
  if (!value || typeof value !== "object") return null;
  const type = value.type === "line" || value.type === "bar" ? value.type : "";
  if (!type || !Array.isArray(value.labels) || !Array.isArray(value.values)
      || !value.labels.length || value.labels.length > 24 || value.labels.length !== value.values.length
      || value.labels.some((label) => typeof label !== "string" || label.length > 40)
      || value.values.some((number) => typeof number !== "number" || !Number.isFinite(number))) return null;
  const chart = {type, labels: value.labels.map((label) => label.trim()), values: value.values.slice()};
  if (typeof value.unit === "string" && value.unit.length <= 30 && value.unit.trim()) chart.unit = value.unit.trim();
  return chart;
}
function cleanCanvasImage(value) {
  if (!value || typeof value !== "object" || !MARK_FILE_ID.test(String(value.file_id || ""))
      || !MARK_AGENT_ID.test(String(value.file_agent || ""))
      || (value.alt !== undefined && (typeof value.alt !== "string" || value.alt.length > 160))) return null;
  return {file_id: String(value.file_id), file_agent: String(value.file_agent),
    alt: String(value.alt || "").trim()};
}
function cleanBoard(value, context) {
  const board = value && typeof value === "object" ? value : {};
  const text = (value_, limit) => String(value_ || "").trim().slice(0, limit);
  const sections = [];
  let total = 0;
  for (const section of Array.isArray(board.sections) ? board.sections : []) {
    if (sections.length >= 8 || !section || !text(section.title, 60)) continue;
    const items = [];
    for (const item of Array.isArray(section.items) ? section.items : []) {
      if (total >= 24 || !item || !text(item.label, 120)) continue;
      const state_ = BOARD_STATES.includes(item.state) ? item.state : "info";
      items.push({label: text(item.label, 120), value: text(item.value, 80),
        detail: text(item.detail, 300), state: state_});
      total += 1;
    }
    const clean = {title: text(section.title, 60), items};
    const chart = cleanCanvasChart(section.chart);
    const image = cleanCanvasImage(section.image);
    if (chart) clean.chart = chart;
    if (image) clean.image = image;
    sections.push(clean);
  }
  return {title: text(board.title, 80) || ((context && context.project) || "Canvas"),
    reporter: text(board.reporter, 80) || "human", updated_at: String(board.updated_at || ""), sections};
}
function boardDraft(context) { return context && state.board.edits.get(context.key); }
function setBoardDraft(context, value) {
  if (context) state.board.edits.set(context.key, value);
}
function boardTime(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "";
}
function scheduleBoardPoll() {
  clearTimeout(state.board.poll);
  if (state.ui.dock !== "board" || document.hidden || !boardContext()) return;
  state.board.poll = setTimeout(() => void loadBoard(), BOARD_POLL_MS);
}
async function loadBoard(force) {
  const context = boardContext();
  if (!context) { state.board.data = null; state.board.key = ""; renderBoardPanel(); return; }
  if (state.board.loading && !force) return;
  // Do not briefly paint the prior room's board while this context is loading.
  if (state.board.key !== context.key) {
    state.board.key = context.key;
    state.board.version = 0;
    state.board.data = null;
    state.board.conflict = null;
  }
  const seq = ++state.board.seq;
  state.board.loading = true;
  if (state.ui.dock === "board") renderBoardPanel();
  try {
    const envelope = await api(boardPath(context), {absolute: true});
    const current = boardContext();
    if (seq !== state.board.seq || !current || current.key !== context.key) return;
    state.board.key = context.key;
    state.board.version = Number(envelope && envelope.version) || 0;
    state.board.data = envelope && envelope.board ? cleanBoard(envelope.board, context) : null;
    state.board.conflict = null;
  } catch (error) {
    const current = boardContext();
    if (seq !== state.board.seq || !current || current.key !== context.key) return;
    state.board.key = context.key;
    state.board.data = null;
    state.board.conflict = {message: error.message || "Could not read this canvas", read: true};
  } finally {
    if (seq === state.board.seq) state.board.loading = false;
    if (state.ui.dock === "board") { renderBoardPanel(); scheduleBoardPoll(); }
  }
}
function boardField(value, label, limit, change) {
  const input = el("input", {type: "text", maxlength: String(limit), value: value || "", "aria-label": label});
  input.value = value || "";
  input.addEventListener("input", () => change(input.value));
  return input;
}
const SVG_NS = "http://www.w3.org/2000/svg";
function chartSvgNode(name, attrs, text) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}
function canvasChart(chart) {
  const unit = chart.unit ? " " + chart.unit : "";
  const valuesText = chart.labels.map((label, at) => label + " " + chart.values[at] + unit).join(" · ");
  const summary = (chart.type === "line" ? "Line" : "Bar") + " chart: " + valuesText;
  const figure = el("figure", {class: "canvas-chart"});
  const svg = chartSvgNode("svg", {viewBox: "0 0 300 118", role: "img", "aria-label": summary});
  svg.appendChild(chartSvgNode("title", {}, summary));
  const left = 26, right = 288, top = 10, bottom = 78;
  const rawMin = Math.min(...chart.values), rawMax = Math.max(...chart.values);
  const min = chart.type === "bar" ? Math.min(0, rawMin) : rawMin;
  const max = chart.type === "bar" ? Math.max(0, rawMax) : rawMax;
  const range = max === min ? 1 : max - min;
  const y = (value) => bottom - ((value - min) / range) * (bottom - top);
  const zero = y(0);
  svg.appendChild(chartSvgNode("line", {x1: left, y1: bottom, x2: right, y2: bottom, class: "canvas-axis"}));
  if (chart.type === "bar") {
    const width = (right - left) / chart.values.length;
    chart.values.forEach((value, at) => {
      const valueY = y(value), x = left + at * width + Math.max(1, width * .13);
      svg.appendChild(chartSvgNode("rect", {x, y: Math.min(zero, valueY),
        width: Math.max(2, width * .74), height: Math.max(1, Math.abs(zero - valueY)), class: "canvas-bar"}));
    });
  } else {
    const step = chart.values.length === 1 ? 0 : (right - left) / (chart.values.length - 1);
    const points = chart.values.map((value, at) => (left + at * step) + "," + y(value)).join(" ");
    svg.appendChild(chartSvgNode("polyline", {points, class: "canvas-line"}));
    chart.values.forEach((value, at) => svg.appendChild(chartSvgNode("circle", {
      cx: left + at * step, cy: y(value), r: 2.5, class: "canvas-point"})));
  }
  const shown = chart.labels.length === 1 ? [0] : [...new Set([0, Math.floor((chart.labels.length - 1) / 2), chart.labels.length - 1])];
  const step = chart.labels.length === 1 ? 0 : (right - left) / (chart.labels.length - 1);
  for (const at of shown) {
    svg.appendChild(chartSvgNode("text", {x: left + at * step, y: 98, "text-anchor": "middle", class: "canvas-chart-label"}, chart.labels[at]));
  }
  figure.appendChild(svg);
  const details = el("details", {class: "canvas-chart-values"});
  details.append(el("summary", {text: "Chart values"}),
    el("p", {text: valuesText}));
  figure.appendChild(details);
  return figure;
}
function canvasImage(image) {
  const slot = el("a", {class: "canvas-image", href: agentPath(image.file_agent,
    "/api/atlas/files/" + image.file_id + "/download"), download: "", title: "Download image"});
  const picture = el("img", {src: agentPath(image.file_agent, "/api/atlas/files/" + image.file_id + "/preview"),
    alt: image.alt || "Canvas image"});
  picture.addEventListener("error", () => slot.remove(), {once: true});
  slot.appendChild(picture);
  return slot;
}
function renderBoardEdit(context, draft) {
  const body = $("#boardBody"), acts = $("#boardActs");
  body.replaceChildren(); acts.replaceChildren();
  const form = el("div", {class: "board-edit"});
  form.append(el("label", {text: "Title"}), boardField(draft.title, "Canvas title", 80, (value) => { draft.title = value; }));
  form.append(el("label", {text: "Updated by"}), boardField(draft.reporter, "Reporter", 80, (value) => { draft.reporter = value; }));
  draft.sections.forEach((section, sectionAt) => {
    const card = el("section", {class: "board-edit-section"});
    const heading = el("div", {class: "board-edit-heading"});
    heading.append(boardField(section.title, "Section title", 60, (value) => { section.title = value; }),
      el("button", {class: "linkbtn", type: "button", text: "Remove", on: {click: () => {
        draft.sections.splice(sectionAt, 1); renderBoardPanel();
      }}}));
    card.appendChild(heading);
    section.items.forEach((item, itemAt) => {
      const row = el("div", {class: "board-edit-item"});
      row.append(boardField(item.label, "Item", 120, (value) => { item.label = value; }),
        boardField(item.value, "Value", 80, (value) => { item.value = value; }));
      const state_ = el("select", {"aria-label": "Item state"});
      for (const name of BOARD_STATES) state_.appendChild(el("option", {value: name, text: name}));
      state_.value = item.state || "info"; state_.addEventListener("change", () => { item.state = state_.value; });
      const detail = el("textarea", {maxlength: "300", placeholder: "Detail", "aria-label": "Item detail"});
      detail.value = item.detail || ""; detail.addEventListener("input", () => { item.detail = detail.value; });
      row.append(state_, detail, el("button", {class: "linkbtn closebtn", type: "button", "aria-label": "Remove item",
        on: {click: () => { section.items.splice(itemAt, 1); renderBoardPanel(); }}}, [useIcon("i-close")]));
      card.appendChild(row);
    });
    card.appendChild(el("button", {class: "linkbtn", type: "button", text: "Add item", on: {click: () => {
      if (draft.sections.reduce((sum, entry) => sum + entry.items.length, 0) >= 24) return;
      section.items.push({label: "", value: "", detail: "", state: "todo"}); renderBoardPanel();
    }}}));
    form.appendChild(card);
  });
  body.appendChild(form);
  acts.append(el("button", {class: "ghost", type: "button", text: "Add section", on: {click: () => {
    if (draft.sections.length < 8) { draft.sections.push({title: "", items: []}); renderBoardPanel(); }
  }}}), el("button", {class: "primary", type: "button", text: state.board.saving ? "Saving…" : "Save",
    disabled: state.board.saving, on: {click: () => void saveBoard(context)}}),
  el("button", {class: "linkbtn", type: "button", text: "Cancel", on: {click: () => {
    state.board.edits.delete(context.key); state.board.conflict = null; renderBoardPanel();
  }}}));
}
function renderBoardPanel() {
  const context = boardContext(), body = $("#boardBody"), acts = $("#boardActs");
  if (!body || !acts) return;
  // Refresh data without forgetting what the reader opened. Capture the
  // rendered room, which can differ from the room now loading after a switch.
  const renderedKey = body.dataset.boardContext;
  if (renderedKey) {
    const remembered = state.board.disclosures.get(renderedKey) || new Map();
    for (const row of body.querySelectorAll("details[data-board-item]")) {
      remembered.set(row.dataset.boardItem, row.open);
    }
    state.board.disclosures.set(renderedKey, remembered);
  }
  body.dataset.boardContext = context ? context.key : "";
  $("#boardContext").textContent = context ? context.project + " · " + context.session : "Choose a conversation";
  const edit = boardDraft(context);
  $("#boardEdit").hidden = !context || !state.board.data || Boolean(edit);
  if (edit) { renderBoardEdit(context, edit); return; }
  body.replaceChildren(); acts.replaceChildren();
  const conflict = state.board.conflict;
  $("#boardNote").textContent = conflict ? conflict.message
    : state.board.loading && !state.board.data ? "Reading canvas…" : "";
  if (!context) { body.appendChild(el("p", {class: "board-empty", text: "Open a conversation to see its canvas."})); return; }
  const board = state.board.data;
  if (!board) {
    body.appendChild(el("p", {class: "board-empty", text: "No canvas yet. Keep the next few things visible here without leaving the conversation."}));
    acts.appendChild(el("button", {class: "primary", type: "button", text: "Start a canvas", on: {click: () => {
      setBoardDraft(context, blankBoard(context)); state.board.conflict = null; renderBoardPanel();
    }}}));
    return;
  }
  body.appendChild(el("h3", {class: "board-title", text: board.title}));
  const remembered = state.board.disclosures.get(context.key) || new Map();
  const occurrences = new Map();
  for (const section of board.sections) {
    const group = el("section", {class: "board-section"});
    group.appendChild(el("h4", {text: section.title}));
    if (section.chart) {
      const chart = canvasChart(section.chart), details = chart.querySelector("details");
      const identity = JSON.stringify([section.title, "chart"]);
      const occurrence = occurrences.get(identity) || 0;
      occurrences.set(identity, occurrence + 1);
      const key = "chart:" + JSON.stringify([identity, occurrence]);
      details.dataset.boardItem = key;
      details.open = remembered.get(key) || false;
      group.appendChild(chart);
    }
    if (section.image) group.appendChild(canvasImage(section.image));
    if (!section.items.length) group.appendChild(el("p", {class: "board-empty", text: "Nothing listed."}));
    for (const item of section.items) {
      const identity = JSON.stringify([section.title, item.label]);
      const occurrence = occurrences.get(identity) || 0;
      occurrences.set(identity, occurrence + 1);
      const key = JSON.stringify([identity, occurrence]);
      const row = el("details", {class: "board-item",
        open: remembered.has(key) ? remembered.get(key) : !item.detail});
      row.dataset.boardItem = key;
      const summary = el("summary");
      summary.append(el("span", {class: "board-state " + (BOARD_STATES.includes(item.state) ? item.state : "info")}),
        el("span", {class: "board-label", text: item.label}),
        item.value ? el("span", {class: "board-value", text: item.value}) : el("span"));
      row.appendChild(summary);
      if (item.detail) row.appendChild(el("p", {text: item.detail}));
      group.appendChild(row);
    }
    body.appendChild(group);
  }
  const meta = [board.reporter ? "updated by " + board.reporter : "", boardTime(board.updated_at)].filter(Boolean).join(" · ");
  if (meta) body.appendChild(el("p", {class: "board-meta", text: meta}));
}
function editBoard() {
  const context = boardContext(); if (!context) return;
  setBoardDraft(context, cloneBoard(state.board.data || blankBoard(context)));
  state.board.conflict = null; renderBoardPanel();
}
async function saveBoard(context) {
  const draft = boardDraft(context); if (!draft || state.board.saving) return;
  const board = cleanBoard(draft, context);
  delete board.updated_at;
  state.board.saving = true; renderBoardPanel();
  try {
    const envelope = await api(boardPath(context), {absolute: true, method: "PUT", body: {
      base_version: state.board.version, board,
    }});
    const current = boardContext();
    if (!current || current.key !== context.key) return;
    state.board.key = context.key; state.board.version = Number(envelope.version) || 0;
    state.board.data = envelope.board ? cleanBoard(envelope.board, context) : board;
    state.board.edits.delete(context.key); state.board.conflict = null;
  } catch (error) {
    const current = boardContext();
    if (!current || current.key !== context.key) return;
    if (error.code === "board_conflict" && error.detail) {
      state.board.version = Number(error.detail.version) || state.board.version;
      state.board.data = error.detail.board ? cleanBoard(error.detail.board, context) : null;
      state.board.conflict = {message: "Someone updated this canvas first. Your edits are still here; save again to apply them."};
    } else state.board.conflict = {message: error.message || "Could not save this canvas"};
  } finally { state.board.saving = false; renderBoardPanel(); }
}
$("#boardEdit").addEventListener("click", editBoard);
window.addEventListener("focus", () => { if (state.ui.dock === "board") void loadBoard(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.ui.dock === "board") void loadBoard();
  if (document.hidden) clearTimeout(state.board.poll);
});

/* Reading controls live with the room details, and say what they will do. */
function renderReadingControls() {
  $("#moreFold").setAttribute("aria-pressed", String(!state.folded));
  $("#moreFold").setAttribute("aria-expanded", String(!state.folded));
  $("#moreFold").replaceChildren(
    el("span", {class: "rn", text: state.folded ? "Show work" : "Hide work"}),
    el("span", {class: "rm", text: state.folded ? "Steps and tool results are hidden" : "Steps and tool results are shown"}));
  $("#moreEarlier").replaceChildren(
    el("span", {class: "rn", text: "Load earlier"}),
    el("span", {class: "rm", text: state.cursor
      ? "Earlier messages are available" : "You’re at the beginning"}));
  const focused = document.body.classList.contains("focus-mode");
  $("#moreFocus").setAttribute("aria-pressed", String(focused));
  $("#moreFocus").replaceChildren(
    el("span", {class: "rn", text: "Focus mode"}),
    el("span", {class: "rm", text: focused
      ? "Conversation only" : "Hide navigation while you read"}));
}

/* After a room switch every open panel must describe the room now on screen. */
function refreshPanelForRoom() {
  if (!state.ui.dock) return;
  // The register reads every agent and refreshes on its own bounded timer. A
  // room switch only changes which single row may be drawn as working, so it
  // redraws rather than asking every host again.
  if (state.ui.dock === "attention") { renderAttentionPanel(); return; }
  renderPanel(true);
}

function toggleFold() {
  state.folded = !state.folded;
  state.openGroups = {};      // all loaded transcript work follows this choice
  state.openWork = {};
  state.openTurns = {};
  renderWorkToggle();
  redrawKeepingPlace();
  renderActivity();
}

/* Where the reader is, so a change of layout can put them back. The anchor is
   the first entry still on screen and how far down it sits — not a scroll
   offset, which means nothing once the column has changed width. */
function readingAnchor() {
  const stream = $("#stream");
  if (!stream) return null;
  const top = stream.getBoundingClientRect().top;
  for (const node of stream.querySelectorAll("#thread > *")) {
    const box = node.getBoundingClientRect();
    if (box.bottom > top + 4) return {node, offset: box.top - top};
  }
  return null;
}

function restoreReadingAnchor(anchor) {
  const stream = $("#stream");
  if (!stream || !anchor || !anchor.node.isConnected) return;
  const top = stream.getBoundingClientRect().top;
  stream.scrollTop += anchor.node.getBoundingClientRect().top - top - anchor.offset;
}

/* Focus is a reading mode, not a layout preference. It never writes one
   either: the drawer and the dock keep their own state untouched underneath,
   so leaving focus restores exactly what was there rather than a guess at it.
   The place in the conversation is carried across by hand, because the column
   changes width on the way in and on the way out. */
function toggleFocus(want) {
  const on = want === undefined ? !state.ui.focus : Boolean(want);
  if (on === Boolean(state.ui.focus)) return;
  const following = state.following;
  const anchor = readingAnchor();

  state.ui.focus = on;
  document.body.classList.toggle("focus-mode", on);
  applyShell();
  syncFloaters();

  const exit = $("#btnFocusExit");
  if (exit) exit.hidden = !on;
  const button = $("#btnFocus");
  if (button) button.setAttribute("aria-pressed", String(on));
  const label = $("#focusLabel");
  if (label) label.textContent = on ? "Exit focus" : "Focus";

  // The column is a different width now. Put the reader back where they were,
  // or keep them at the live edge if that is where they were.
  requestAnimationFrame(() => {
    if (following) { toTail(); state.following = true; }
    else restoreReadingAnchor(anchor);
  });

  // Focus goes somewhere sensible and never onto something now hidden.
  if (on) { if (exit) exit.focus(); }
  else if (button && button.offsetParent !== null) button.focus();
  else $("#draft").focus();
}

/* Work detail is a disclosure: the button says what pressing it will do, and
   both the expanded and the pressed state say the same true thing about the
   detail underneath. */
function renderWorkToggle() {
  const button = $("#btnFold");
  if (!button) return;
  const shown = !state.folded;
  button.textContent = shown ? "Hide work" : "Show work";
  button.setAttribute("aria-expanded", String(shown));
  button.setAttribute("aria-pressed", String(shown));
  button.title = shown
    ? "Hide work across the loaded transcript"
    : "Show work across the loaded transcript";
}

/* ---------------------------------------------------------------- listening */
/* The microphone is a compact control inside the field, so its state is the
   pressed state and its label — never its text, which would erase the icon. */
function markListening(on) {
  const button = $("#btnListen");
  if (!button) return;
  button.setAttribute("aria-pressed", String(on));
  const label = on ? "Stop dictating" : "Dictate";
  button.title = on ? label : button.dataset.hint || label;
  const name = button.querySelector(".sr-only");
  if (name) name.textContent = label;
}

function stopDictation() {
  if (!state.listening) return;
  const {recognition} = state.listening;
  state.listening = null;
  markListening(false);
  try { recognition.stop(); } catch (e) { /* already finished */ }
}

/* Speech recognised for one room must never land in another. The callback can
   arrive after a switch, so it carries the room and generation it began in. */
function applyDictation(text, token) {
  const words = (text || "").trim();
  if (!words) return false;
  if (!token || token.room !== state.room || token.seq !== state.roomSeq) return false;
  const draft = $("#draft");
  if (!draft || draft.disabled) return false;
  draft.value = (draft.value + (draft.value && !draft.value.endsWith(" ") ? " " : "") + words).trim();
  scheduleDraftSave();
  return true;
}

function setupListen() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const button = $("#btnListen");
  if (!Recognition) return;  // the phone keyboard's own dictation still works
  button.hidden = false;
  button.dataset.hint = "Dictate with this browser's own speech recognition. "
    + "Phone keyboard dictation works too. No model call from UX46.";
  button.title = button.dataset.hint;
  button.addEventListener("click", () => {
    if (state.listening) { stopDictation(); return; }
    if (!state.room) return;
    const token = {room: state.room, seq: state.roomSeq};
    const recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      let text = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        text += event.results[i][0].transcript;
      }
      if (!applyDictation(text, token)) stopDictation();
    };
    recognition.onend = () => { state.listening = null; markListening(false); };
    recognition.onerror = () => { state.listening = null; markListening(false); };
    recognition.start();
    state.listening = {recognition, token};
    markListening(true);
  });
}

/* --------------------------------------------------------------- event loop */
async function pollEvents() {
  for (;;) {
    const gen = state.agentGen;
    try {
      const payload = await api("/api/events?after=" + state.seq + "&timeout=25"
        + "&epoch=" + encodeURIComponent(state.eventEpoch));
      // A reply from the agent we just left must not advance this agent's
      // sequence or redraw its rooms.
      if (gen !== state.agentGen) continue;
      const reconnect = state.connKind === "off";
      const reset = payload.gap || (state.eventEpoch && payload.epoch !== state.eventEpoch)
        || payload.seq < state.seq;
      state.eventRecovery = state.eventRecovery || reconnect || reset;
      state.seq = payload.seq;
      state.eventEpoch = payload.epoch || "";
      setConn("reachable", "live");
      // Older adapters cannot prove lossless event delivery. Reconcile their
      // selected snapshot each poll instead of trusting an empty event batch.
      const reconcile = state.eventRecovery || !payload.epoch || viewFreshnessError()
        || Date.now() - state.freshness.room > 30000;
      const roomSeq = state.roomSeq, roomId = state.room;
      let touched = false;
      let lifecycle = false;
      let approvalChanged = false;
      let queueChanged = false;
      // Each bounded page invalidates projections; the next poll immediately
      // continues from its returned cursor when more events remain.
      for (const event of payload.events || []) {
        if (gen !== state.agentGen || stale(roomSeq, roomId)) break;
        if (event.type === "native" && (event.room === state.room
            || (event.rooms || []).includes(state.room))) {
          if (event.terminal) {
            state.connectionRefreshState = "";
            lifecycle = true;
            if (state.detail) {
              state.detail.native_terminal = event.terminal;
              state.detail.account_status = {state: "unknown", checked_at: null};
              state.detail.connection_recovery = {state: "current"};
            }
            if (state.accepted?.turn === event.terminal.turn_id) state.accepted = null;
            renderActivity();
          }
          touched = true;
          if (event.method === "turn/started" || event.method === "turn/completed") {
            lifecycle = true;
            if (event.method === "turn/completed") state.accepted = null;
          }
        }
        if (["submission", "settings", "command", "goal"].includes(event.type) && event.room === state.room) lifecycle = true;
        if (event.type === "queued_message" && event.room === state.room) queueChanged = true;
        // A gateway push carries run state under its own event name rather
        // than turn/started or turn/completed, so it says so directly.
        if (event.lifecycle && event.room === state.room) {
          lifecycle = true;
          if (event.active_run === false) state.accepted = null;
        }
        if (event.type === "goal" && event.room === state.room && state.commandMode === "goal"
            && state.goalPanelTarget?.agent === agentId() && state.goalPanelTarget?.room === state.room) {
          const roomId=state.room, seq=state.roomSeq, target=state.goalPanelTarget;
          const read=await api("/api/room/"+encodeURI(roomId)+"/command",{method:"POST",body:{command:"/goal",client_id:clientId()}});
          if (!stale(seq,roomId) && state.commandMode === "goal" && state.goalPanelTarget === target) {
            const outcome = read.command || {}, native = outcome.native || {};
            state.goalPanel = native.goal || null;
            state.goalPanelStatus = {state: Object.hasOwn(native, "goal") ? "known" : "unknown", goal: state.goalPanel};
            renderCommands();
          }
        }
        if (event.type === "approval") approvalChanged = true;
        if (event.type === "ownership" && event.room === state.room) await refreshRoomState();
        if (event.type === "runtime/exited") setConn("runtime stopped", "off");
      }
      // active_turn lives in the room state, so a turn opening or closing has
      // to be read there rather than guessed from the item stream.
      if (gen !== state.agentGen || stale(roomSeq, roomId)) continue;
      const roomOK = lifecycle || reconcile ? await refreshRoomState() : true;
      if (gen !== state.agentGen || stale(roomSeq, roomId)) continue;
      const historyOK = touched || reconcile ? await refreshTail() : true;
      if (gen !== state.agentGen || stale(roomSeq, roomId)) continue;
      if (reconcile && roomOK && historyOK && !payload.more) {
        state.eventRecovery = false;
        renderActivity(); renderTabs();
      }
      if (queueChanged) {
        await loadPending(state.room);
        redrawKeepingPlace();
        schedulePendingPoll();
      }
      if (approvalChanged) {
        await refreshRoomState();
        if (!$("#viewBoard").hidden) await renderBoard();
        else { await renderBoardCountOnly(); renderStream(); }
        renderTabs();          // the orange dot follows live requests only
        if (state.ui.dock === "attention") loadAttention();
      }
    } catch (error) {
      if (gen === state.agentGen) {
        state.eventRecovery = true;
        setConn(isRemoteAgent() ? agentLabel() + " unavailable" : "offline", "off");
      }
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  }
}

/* ------------------------------------------------------------ agent switching */
/* Choosing an agent changes which runtime this whole app is talking to. It
   never touches that agent's native processes: nothing is attached, resumed or
   released by a switch, so the conversation you leave keeps running exactly as
   it was and is still held when you come back. */

function rememberAgent(clearRoom) {
  try {
    if (isRemoteAgent()) window.localStorage.setItem("atlas.agent", agentId());
    else window.localStorage.removeItem("atlas.agent");
  } catch (e) { /* private mode: the deep link still works */ }
  try {
    const url = new URL(window.location.href);
    if (isRemoteAgent()) url.searchParams.set("agent", agentId());
    else url.searchParams.delete("agent");
    // A room belongs to the agent that owns it, so a switch drops it from the
    // address bar. A page load keeps whatever ?room= it was given.
    if (clearRoom) url.searchParams.delete("room");
    window.history.replaceState(null, "", url.toString());
  } catch (e) { /* nothing depends on the URL */ }
}

async function loadAgents() {
  // Always this console's own list: an agent does not publish other agents.
  const payload = await api("/api/agents", {absolute: true});
  state.agents = payload.agents || [];
  // Resolve the installation's existing local identity before restoring its
  // tabs and drafts. Public defaults must not rename an older installation.
  if (!localAgentResolved) {
    const local = state.agents.find(agent => agent.id === payload.default && agent.kind === "local");
    if (local && /^[a-z][a-z0-9-]{0,31}$/.test(local.id)) DEFAULT_AGENT = local.id;
    localAgentResolved = true;
  }
  return payload;
}

/* Which agent the drawer is reading. Every catalogue request below names it
   explicitly, so a slow answer for one agent can never land as another's. */
function browseAgent() { return state.browse.agent || agentId(); }
function browseApi(path, options) {
  return api(agentPath(browseAgent(), path), Object.assign({absolute: true}, options || {}));
}

/* Look through a different agent without leaving the conversation you are in.
   Nothing about the open room, its draft, its uploads or its tab changes. */
function setBrowseAgent(id) {
  const wanted = id || DEFAULT_AGENT;
  if (browseAgent() === wanted) return;
  state.browse.agent = wanted === DEFAULT_AGENT ? "" : wanted;
  state.rooms = new Map();
  state.prefs = {};
  state.openProjects = new Set();
  invalidateRoomPages();
  $("#roomSearch").value = "";
  renderAgentPicker();
  renderRoomList();
}

function agentAvailability(id) {
  const found = (state.agents || []).find((a) => a.id === (id || agentId()));
  return (found && found.availability) || {state: "unknown", detail: ""};
}

const agentActions={agent:null,anchor:null,job:null,loading:false,poll:null,request:null,generation:0};
function closeAgentActions(){clearTimeout(agentActions.poll);agentActions.generation++;$('#agentActionsDialog').close();if(agentActions.anchor?.isConnected)agentActions.anchor.focus();}
function openAgentActions(id,anchor){
  const agent=state.agents.find(a=>a.id===id);if(!agent)return;
  agentActions.agent=id;agentActions.anchor=anchor;agentActions.job=null;agentActions.request=null;agentActions.generation++;
  $('#agentActionsTitle').textContent=agent.label;
  $('#agentActionsMark').replaceChildren(agentAvatar(agent,{status:agentAbout(agent).off?'off':'ok'}));
  $('#agentActionsAbout').textContent=agent.runtime==='codex'?'Sessions use this agent’s saved Codex login.':agent.runtime+' · '+(agent.node||'');
  $('#agentActionsRefresh').disabled=agent.runtime!=='codex';
  $('#agentActionsHelp').textContent=agent.runtime==='codex'?'Reconnect idle owned sessions using this agent’s saved login. Busy sessions are reported; full recovery can interrupt them.':'Session-wide login refresh is not available for this runtime yet.';
  renderAgentRefresh();$('#agentActionsDialog').showModal();void loadAgentRefresh();
}
function renderAgentRefresh(){
  const job=agentActions.job,host=$('#agentRefreshStatus');host.replaceChildren();
  if(!job){host.textContent=agentActions.loading?'Checking…':'';return;}
  const counts={};for(const item of job.items||[])counts[item.state]=(counts[item.state]||0)+1;
  host.appendChild(el('p',{text:job.message||job.state}));
  if(job.items?.length)host.appendChild(el('p',{class:'agent-refresh-counts',text:[counts.refreshed?counts.refreshed+' refreshed':'',counts.waiting?counts.waiting+' waiting':'',counts.skipped?counts.skipped+' already disconnected':'',(counts.failed||counts.unknown||counts.expired)?'Some need attention':''].filter(Boolean).join(' · ')}));
  if(job.items?.length)host.appendChild(el('details',{},[el('summary',{text:'Session details'}),...job.items.map(item=>el('p',{text:(item.title||state.tabs.find(t=>t.agent===job.agent&&t.room===item.room)?.title||item.room)+' · '+item.state+(item.detail?' — '+item.detail:'')}))]));
  $('#agentActionsRefresh').disabled=['discovering','refreshing','waiting'].includes(job.state);
  if(!['discovering','refreshing','waiting'].includes(job.state))agentActions.request=null;
}
async function loadAgentRefresh(){
  clearTimeout(agentActions.poll);const agent=agentActions.agent,gen=agentActions.generation;
  try{const result=await api('/api/recovery/status',{absolute:true});
    if(gen!==agentActions.generation)return;
    agentActions.job=result.job?.agent===agent ? {...result.job, items:result.job.connections} : null;renderAgentRefresh();
  }catch(error){if(gen===agentActions.generation)$('#agentRefreshStatus').textContent='Refresh status unavailable.';}
}
async function refreshAgentSessions(){
  const agent = agentActions.agent;
  closeAgentActions();
  openWorkspaceRecovery("agent", agent);
  await runWorkspaceRecovery();
}

$('#agentActionsClose').addEventListener('click',closeAgentActions);
$('#agentActionsDialog').addEventListener('close',()=>{clearTimeout(agentActions.poll);agentActions.generation++;});
$('#agentActionsBrowse').addEventListener('click',()=>{const id=agentActions.agent;closeAgentActions();setBrowseAgent(id);});
$('#agentActionsNew').addEventListener('click',()=>{const id=agentActions.agent,anchor=agentActions.anchor;closeAgentActions();openNewConversation(anchor,id,true);});
$('#agentActionsRefresh').addEventListener('click',()=>void refreshAgentSessions());
$('#sessCap').addEventListener('click',event=>openAgentActions(agentId(),event.currentTarget));

function renderAgentPicker() {
  const host = $("#agentPicker");
  if (!host) return;
  host.replaceChildren();
  if (!(state.agents || []).length) { host.hidden = true; return; }
  host.hidden = false;
  for (const agent of state.agents) {
    const availability = agent.availability || {};
    const about = agentAbout(agent);
    const off = about.off;
    const detail = [agent.runtime, agent.node].filter(Boolean).join(" · ");
    const here = agent.id === agentId();
    host.appendChild(el("button", {
      class: "agentbtn" + (off ? " off" : "") + (here ? " here" : ""),
      type: "button", "aria-haspopup":"dialog",
      "aria-pressed": String(agent.id === browseAgent()),
      // The name travels with the button even when the drawer is a rail and
      // the label is not drawn: a face nobody can name is not an identity.
      "aria-label": about.label
        + (here ? " · the conversation you are in" : "")
        + " · actions",
      title: agent.label + (detail ? " · " + detail : "")
        + (here ? " · the conversation you are in" : "")
        + " · actions"
        + (off ? " · unavailable: " + (availability.detail || "not reachable") : ""),
      data: {agent: agent.id},
      on: {click: event => openAgentActions(agent.id,event.currentTarget)},
    }, [agentAvatar(agent, {status: off ? "off" : "ok"}),
        el("span", {class: "albl", text: agent.label})]));
  }
}

/* Nothing that belongs to one agent may be read as another's.

   This clears the conversation, not the catalogue: the tab strip spans every
   agent and stays, and the left drawer keeps listing whichever agent it was
   asked to list. Only `setBrowseAgent` changes that. */
function resetAgentState() {
  roomViews.clear();
  attachmentIndex.clear();
  loadUploadDrafts();
  clearSpeech();
  state.ids = new Set();
  Object.assign(state, {
    eventEpoch: "", eventRecovery: true,
    freshness: {room: 0, history: 0, roomError: "", historyError: ""},
    room: null, detail: null, items: [], tail: [], cursor: null, complete: true,
    sel: null, anchor: null, searchHits: [], newCount: 0, openWork: {}, openGroups: {}, openTurns: {},
    accepted: null, outcome: null, conflict: null, historyUnavailable: null,
    roomError: null, roomGone: false, positions: {},
    attention: null, approvals: [], draft: {body: "", version: 0}, draftDirty: false,
    goalPanel: null, commandMode: "", connectionRefreshState: "", receipt: null,
    pending: [], pendingSupport: "", pendingEdit: null, attach: null,
  });
  state.pendingDismissed = new Set();
  clearTimeout(pendingTimer);
  $("#draft").value = "";
  renderStream();
  renderTabs();
  renderCrumb();
  renderTarget();
  updateNeedsCount();
}

/* Change which agent the console is working in. `wantRoom` is the exact room
   the caller is going to, so nothing else is opened on the way there. */
async function switchAgent(id, wantRoom, opts) {
  if (!id || !configuredIds().has(id)) return false;
  if (id === agentId()) {
    if (wantRoom) return selectRoom(wantRoom, opts || {toTail: true});
    return true;
  }
  stopDictation();
  closeCommands();
  const previous = agentId();
  // History can be visible while its pending-message request is still loading.
  // Save the location before invalidating that request on an agent switch.
  if (state.room && !state.roomGone) rememberRoom(state.room);
  roomSelectionIntent += 1;
  await flushDraft();          // the agent you are leaving keeps your text
  state.agentGen += 1;
  state.roomSeq += 1;          // every reply still in flight is now stale
  state.agent = id === DEFAULT_AGENT ? "" : id;
  rememberAgent(true);
  resetAgentState();
  // The drawer follows the conversation you moved into, unless you had sent it
  // somewhere else on purpose — in which case it stays where you put it.
  if (!state.browse.agent || state.browse.agent === previous) {
    state.browse.agent = id === DEFAULT_AGENT ? "" : id;
    state.rooms = new Map();
    state.prefs = {};
    state.openProjects = new Set();
    invalidateRoomPages();
  }
  renderAgentPicker();
  renderTabs();
  await enterAgent(null, wantRoom, opts);
  return true;
}

/* Open the selected agent, or say plainly that it is not available. There is
   no fallback: another agent's rooms are never shown under this agent's name. */
async function enterAgent(bootstrap, wantRoom, opts) {
  const gen = state.agentGen;
  state.agentError = "";
  setConn("connecting…", "");
  let payload = bootstrap || null;
  if (!payload) {
    try { payload = await api("/api/bootstrap"); }
    catch (error) {
      if (gen !== state.agentGen) return;
      state.agentError = error.message || "not reachable";
      setConn(agentLabel() + " unavailable", "off");
      $("#stream").replaceChildren(el("p", {class: "empty", text:
        agentLabel() + " is not available right now: " + state.agentError
        + " UX46 will not answer for it with another agent — choose a different "
        + "agent, or try again once it is reachable."}));
      setSendState("No agent connection", "fail");
      renderCrumb();
      renderTarget();
      loadAgents().then(renderAgentPicker).catch(() => {});
      return;
    }
  }
  if (gen !== state.agentGen) return;
  state.node = payload.node || "";
  state.seq = payload.seq || 0;
  state.eventEpoch = "";
  state.eventRecovery = true;
  const voice = payload.voice || {};
  let preferred = "";
  try { preferred = window.localStorage.getItem("atlas.voice") || ""; } catch (e) { preferred = ""; }
  state.voice = {
    enabled: !!voice.enabled,
    voices: voice.voices || [],
    reason: voice.reason || "",
    preferred: (voice.voices || []).includes(preferred) ? preferred
      : (payload.voice_default || (voice.voices || [])[0] || ""),
  };
  renderVoicePicker();
  setConn(state.mode === "private-network" ? "private network" : "local", "live");

  // The room is restored through this agent's own API, so a session that sits
  // outside the first catalogue page still comes back. A caller that named one
  // wins; then a deep link, then what this agent had open, then its first tab.
  let wanted = wantRoom || "";
  if (!wanted) {
    try {
      const params = new URLSearchParams(window.location.search);
      wanted = params.get("room") || "";
    } catch (e) { wanted = ""; }
  }
  if (!wanted) {
    try { wanted = window.localStorage.getItem(agentKey("atlas.room")) || ""; } catch (e) { wanted = ""; }
  }
  if (!wanted) {
    const mine = state.tabs.find((tab) => tab.agent === agentId());
    if (mine) wanted = mine.room;
  }

  // Arriving at the conversation you were in is a selection, so it connects
  // like one. The suggestion below, chosen because there was nowhere to be,
  // is not: nobody asked for it.
  let restored = false;
  if (wanted) {
    restored = await selectRoom(wanted, opts || {toTail: true, connect: true});
    if (gen !== state.agentGen) return;
    if (!restored && state.roomGone) {
      flash("The room you had open is no longer in the registry, so UX46 opened another.");
      rememberRoom("");
    }
  }

  renderTabs();

  // Only when there is nowhere to be. The suggestion is read from this agent
  // directly rather than from the drawer's cache, which may be listing
  // somebody else.
  if (!wanted || (!restored && state.roomGone)) {
    try {
      const workspace = await api("/api/workspace");
      if (gen !== state.agentGen) return;
      const suggested = workspace.projects
        .flatMap((project) => project.pinned.concat(project.suggested));
      const fallback = (suggested.find((room) => room.controllable) || suggested[0] || {}).id;
      if (fallback) await selectRoom(fallback, {toTail: true});
      else if (!state.room) {
        $("#crumb").textContent = "Your workspace is ready";
        const start = el("button", {class: "primary", text: "Start a conversation"});
        start.addEventListener("click", () => openNewConversation(start));
        $("#stream").replaceChildren(el("p", {class: "empty", text:
          "Start fresh with your agent. Name conversations and file them into projects as you work."}), start);
        setSendState("Start a conversation to begin", "");
      }
    } catch (error) {
      if (!state.room) setSendState("Could not list projects: " + error.message, "fail");
    }
  }
  if (gen !== state.agentGen) return;
  if (browseAgent() === agentId()) { invalidateRoomPages(); renderRoomList(); }
  await renderBoardCountOnly();
}

/* --------------------------------------------------------------------- boot */
async function boot() {
  state.device = deviceId();      // one device identity, shared by every agent
  state.ui.left = readPref("atlas.left") === "rail" ? "rail" : "wide";
  const dockPref = readPref("atlas.dock");
  // A remembered panel only reopens where it docks beside the console.
  if (isWide() && ["attention", "turns", "updates", "board", "notes"].includes(dockPref)) {
    state.ui.dock = dockPref;
  } else if (!dockPref && window.matchMedia("(min-width:1400px)").matches) {
    // Wide enough that the register costs the conversation nothing. Narrower
    // than that — and on any phone, with or without a keyboard — it stays shut
    // until someone opens it.
    state.ui.dock = "attention";
    state.ui.overlay = null;
  }
  applyShell();
  renderPanel(true);

  let bootstrap = null;
  try {
    // This console's own boot state. The CSRF token is always the local one:
    // a remote agent's token is obtained server side and never reaches here.
    bootstrap = await api("/api/bootstrap", {absolute: true});
    state.csrf = bootstrap.csrf;
    state.mode = bootstrap.mode;
  } catch (error) {
    setConn("denied", "off");
    $("#stream").replaceChildren(el("p", {class: "empty",
      text: "This console refused the request: " + error.message}));
    return;
  }

  try { await loadAgents(); } catch (error) { state.agents = []; }

  // A deep link chooses the agent, then the workspace's own layout, then this
  // device's last choice.
  let linkAgent = "";
  let linkRoom = "";
  try {
    const params = new URLSearchParams(window.location.search);
    linkAgent = params.get("agent") || "";
    linkRoom = params.get("room") || "";
  } catch (e) { linkAgent = ""; linkRoom = ""; }
  let wantedAgent = linkAgent;
  if (!wantedAgent) {
    try { wantedAgent = window.localStorage.getItem("atlas.agent") || ""; } catch (e) { wantedAgent = ""; }
  }
  if (!wantedAgent && !state.agents.some(a => a.id === DEFAULT_AGENT) && state.agents.length) wantedAgent=state.agents[0].id;

  state.tabs = readTabs();
  // Display names and the workspace's own arrangement both come from the
  // store, so it is read before the strip is drawn — and, more to the point,
  // before anything on this device writes a strip of its own over it.
  await loadDesktopState();
  let wantedRoom = "";
  const layout = deviceOnlyLayout() ? null : layoutRecord(state.desk.raw);
  if (layout && !layout.tooNew) {
    await applySharedLayout(layout, {first: true});
    // A link says where to go; otherwise the workspace's own open conversation
    // does, which is what makes a new device arrive where the others already
    // are. Either way it is opened by the ordinary read, once, below.
    if (!linkRoom && layout.active) {
      wantedRoom = layout.active.room;
      if (!linkAgent) wantedAgent = layout.active.agent;
    }
  }
  if (wantedAgent && wantedAgent !== DEFAULT_AGENT
      && (state.agents || []).some((a) => a.id === wantedAgent)) {
    state.agent = wantedAgent;
  } else if (wantedAgent === DEFAULT_AGENT) {
    state.agent = "";
  }
  // The layout named an agent this console could not select, so its room is
  // not this agent's to open.
  if (wantedRoom && layout && layout.active && layout.active.agent !== agentId()) wantedRoom = "";
  rememberAgent(false);
  state.browse.agent = state.agent;
  loadUploadDrafts();
  renderAgentPicker();
  renderTabs();
  // The register was drawn before the agent list arrived, so if it is the open
  // panel it now has every agent to read rather than only this one.
  if (state.ui.dock === "attention") loadAttention();

  await enterAgent(isRemoteAgent() ? null : bootstrap, wantedRoom);
  void checkDesktopDevice();scheduleLayoutPoll();
  // Once per page load, across every agent — not again on each switch.
  await recoverAttempts();
  pollEvents();
}

async function renderBoardCountOnly() {
  try {
    const payload = await api("/api/attention");
    state.attention = payload;
    state.approvals = payload.approvals || [];
    updateNeedsCount();
  } catch (error) { /* the board itself will report the failure */ }
}

/* ----------------------------------------------------- native slash controls */
const COMMANDS = [
  ["/model", "Choose the model for this session"],
  ["/effort", "Choose reasoning effort · /reasoning also works"],
  ["/steer", "Send direction to the currently running turn"],
  ["/new", "Next chapter on updated Codex connectors; new conversation on other runtimes"],
  ["/goal", "Inspect, pause or resume this session’s saved goal"],
  ["/status", "Show current native session settings"],
  ["/compact", "Ask Codex to compact this session"],
  ["/refresh", "Reload UX46’s connection after an account switch"],
  ["/help", "Show supported commands"],
];
function commandName(text) {
  const name = text.trim().split(/\s+/, 1)[0].toLowerCase();
  return ({"/streer":"/steer", "/reasononing":"/effort", "/reasoning":"/effort"})[name] || name;
}
function commandSupported(name) {
  const advertised = state.detail?.commands;
  return !Array.isArray(advertised) || advertised.some(entry => commandName(entry.name || entry.usage || "") === name);
}
function commandMessage(outcome) {
  return outcome.message || outcome.reason || outcome.output || "";
}
function modelCatalog() {
  const catalog = (state.detail && state.detail.native || {}).catalog || [];
  return Array.isArray(catalog) ? catalog : (catalog.models || catalog.data || []);
}
function renderModelControl() {
  const button = $("#btnModel"), n = state.detail && state.detail.native;
  button.hidden = !n;
  if (!n) return;
  button.textContent = (n.model || "Model unavailable") + " · " + (n.reasoning_effort || "effort unavailable") + " ▾";
  button.title = "Current session settings from the agent. Changes apply to subsequent turns.";
  button.disabled = state.sending || state.roomRefreshing;
}
function closeCommands() {
  state.commandMode = "";
  $("#commandPopover").hidden = true;
  $("#btnModel").setAttribute("aria-expanded", "false");
  $("#btnGoal").setAttribute("aria-expanded", "false");
}
function showCommands(mode, fromDraft) {
  state.commandMode = mode || "commands";
  state.commandFromDraft = !!fromDraft;
  renderCommands();
  if ((mode === "model" || mode === "effort") && !modelCatalog().length) void loadCommandCatalog();
}
async function loadCommandCatalog() {
  if (state.catalogLoading || !state.detail) return;
  const roomId=state.room, seq=state.roomSeq, owner=agentId(), gen=state.agentGen;
  const changed=()=>stale(seq,roomId) || owner!==agentId() || gen!==state.agentGen;
  state.catalogLoading=true;
  try {
    const result=await api(agentPath(owner,"/api/room/"+encodeURI(roomId)+"/command"),{absolute:true,method:"POST",body:{command:"/model",client_id:clientId(),thread_id:(state.detail.native || {}).thread_id}});
    if (changed()) return;
    const catalog=(result.command || {}).catalog || (result.room && result.room.native || {}).catalog || [];
    if (state.detail.native) state.detail.native.catalog=catalog;
    if (state.commandMode) renderCommands();
  } catch(error) {if(!changed())setReceipt(error.message,"warn");}
  finally {state.catalogLoading=false;}
}
function renderGoalResumeControl() {
  const button = $("#btnGoalResume");
  if (!button) return;
  const target = state.goalPanelTarget, native = state.detail?.native;
  const alreadyWorking = target?.agent === agentId() && target?.room === state.room
    && state.goalPanelStatus?.state === "known" && state.goalPanel?.status === "active"
    && native?.active_turn && native.active_turn !== state.detail?.native_terminal?.turn_id;
  button.disabled = Boolean(alreadyWorking);
  button.textContent = alreadyWorking ? "Already working" : "Resume goal";
}
function positionGoalMenu() {
  const popup = $("#commandPopover");
  if (popup.hidden || state.commandMode !== "goal") return;
  const anchor = goalMenuAnchor?.isConnected ? goalMenuAnchor : $("#btnGoal");
  const rect = anchor.getBoundingClientRect(), margin = 8;
  const viewport = window.visualViewport;
  const leftEdge = (viewport?.offsetLeft || 0) + margin;
  const topEdge = (viewport?.offsetTop || 0) + margin;
  const rightEdge = leftEdge + (viewport?.width || window.innerWidth) - margin * 2;
  const bottomEdge = topEdge + (viewport?.height || window.innerHeight) - margin * 2;
  popup.style.width = Math.min(420, rightEdge - leftEdge) + "px";
  popup.style.left = Math.max(leftEdge, Math.min(rect.left, rightEdge - popup.offsetWidth)) + "px";
  const below = bottomEdge - rect.bottom - margin, above = rect.top - topEdge - margin;
  const down = below >= Math.min(popup.scrollHeight, 260) || below >= above;
  popup.style.maxHeight = Math.max(60, Math.min(360, down ? below : above)) + "px";
  popup.style.top = Math.max(topEdge, down ? rect.bottom + margin : rect.top - popup.offsetHeight - margin) + "px";
}
window.addEventListener("resize", positionGoalMenu);
document.addEventListener("scroll", event => {
  if (!$("#commandPopover").contains(event.target)) positionGoalMenu();
}, true);
window.visualViewport?.addEventListener("resize", positionGoalMenu);
function renderCommands() {
  const host = $("#commandOptions"), mode = state.commandMode;
  if (!mode) { closeCommands(); return; }
  const popup = $("#commandPopover"); popup.hidden = false;
  popup.classList.toggle("goal-menu", mode === "goal");
  if (mode !== "goal") for (const property of ["left", "top", "width", "max-height"]) popup.style.removeProperty(property);
  $("#btnModel").setAttribute("aria-expanded", String(mode !== "goal"));
  $("#btnGoal").setAttribute("aria-expanded", String(mode === "goal" && state.goalPanelTarget?.agent === agentId() && state.goalPanelTarget?.room === state.room));
  $("#commandTitle").textContent = mode === "model" ? "Model" : mode === "effort" ? "Reasoning effort" : mode === "goal" ? "Session goal" : "Session commands";
  host.replaceChildren();
  const native = state.detail && state.detail.native || {};
  if (mode === "goal") {
    const goal=state.goalPanel, status=state.goalPanelStatus || {state: "unknown", goal: null};
    const target=state.goalPanelTarget;
    const current=target?.agent === agentId() && target?.room === state.room;
    if (target && !current) host.appendChild(el("p", {class:"command-note", text: agentLabel(target.agent) + " · " + target.room}));
    host.appendChild(el("p",{class:"command-note",text:goal ? (goal.objective || goal.text || "Saved goal")
      : status.state === "known" ? "No saved goal in this session."
      : status.state === "unsupported" ? "Goals are unavailable for this session." : "Goal status is unknown."}));
    if (status.message) host.appendChild(el("p", {class:"command-note", text: status.message}));
    if (goal) {
      host.appendChild(el("p",{class:"command-note",text:(status.state === "known" ? "State: " : "Last known state: ") + (goal.status || "unavailable")}));
      if (current && status.state === "known") host.appendChild(el("div",{class:"command-tabs"},[
        el("button",{id:"btnGoalResume",type:"button",class:"linkbtn",text:"Resume goal",on:{click:()=>sendCommand("/goal resume",false)}}),
        el("button",{type:"button",class:"ghost",text:"Pause goal",on:{click:()=>sendCommand("/goal pause",false)}}),
      ]));
      renderGoalResumeControl();
      if (current && status.state === "known") host.appendChild(el("p",{class:"command-note",text:"Resumes the existing goal. Account usage limits and workspace permissions remain separate."}));
    }
  } else if (mode === "model" || mode === "effort") {
    host.appendChild(el("div", {class:"command-tabs"}, [
      el("button", {type:"button",class:"ghost",text:"Model",on:{click:()=>showCommands("model",state.commandFromDraft)}}),
      el("button", {type:"button",class:"ghost",text:"Effort",on:{click:()=>showCommands("effort",state.commandFromDraft)}}),
    ]));
    const catalog = modelCatalog();
    const current = catalog.find(m => (m.model || m.id) === native.model);
    const values = mode === "model" ? catalog.map(m=>({value:m.model || m.id,label:m.display_name || m.displayName || m.model || m.id,description:m.description || ""}))
      : ((current && (current.efforts || current.supportedReasoningEfforts)) || []).map(e=>({value:e.reasoningEffort || e.effort || e,label:e.reasoningEffort || e.effort || e,description:e.description || ""}));
    for (const value of values) host.appendChild(el("button", {class:"command-choice",type:"button",on:{click:()=>sendCommand((mode === "model" ? "/model " : "/effort ") + value.value,state.commandFromDraft)}}, [
      el("b", {text:value.label + (value.value === (mode === "model" ? native.model : native.reasoning_effort) ? " ✓" : "")}),
      el("span", {text:value.description}),
    ]));
    host.appendChild(el("p", {class:"command-note",text:values.length ? "Changes apply to subsequent turns. Any running turn keeps its settings." : "This agent has not reported a list of options. You can enter a value below."}));
    if (!values.length && commandSupported(mode === "model" ? "/model" : "/effort")) {
      const input = el("input", {type:"text", "aria-label": mode === "model" ? "Model name" : "Reasoning effort", placeholder: mode === "model" ? "Model name" : "Effort level", maxlength:"64"});
      const apply = () => { const value=input.value.trim(); if(value) void sendCommand((mode === "model" ? "/model " : "/effort ")+value,state.commandFromDraft); };
      input.addEventListener("keydown", event => {if(event.key === "Enter"){event.preventDefault();apply();}});
      host.appendChild(el("div", {class:"command-tabs"}, [input, el("button",{class:"linkbtn",type:"button",text:"Apply",on:{click:apply}})]));
    }
  } else {
    const text = $("#draft").value.trim();
    const query = text.startsWith("/") ? commandName(text) : "/";
    const choices = COMMANDS.filter(([name])=>name.startsWith(query) || query === "/help");
    for (const [name, description] of choices) host.appendChild(el("button", {class:"command-choice",type:"button",on:{click:()=>{
      if ((name === "/model" || name === "/effort") && commandSupported(name)) { showCommands(name.slice(1),true); return; }
      $("#draft").value = name + " "; scheduleDraftSave(); closeCommands(); $("#draft").focus(); renderTarget();
    }}}, [el("b",{text:name}),el("span",{text:commandSupported(name) ? description : "Not available on this connector"})]));
    if (!choices.length) host.appendChild(el("p", {class:"command-note",text:"Type /help to see this agent’s commands. Slash commands are never sent as ordinary messages."}));
  }
  if (mode === "goal") positionGoalMenu();
}
function commandDraftChanged() {
  const text = $("#draft").value.trim();
  if (!text.startsWith("/")) { closeCommands(); renderTarget(); return; }
  const name = commandName(text);
  if ((name === "/model" || name === "/effort") && commandSupported(name) && !/\s+\S/.test(text)) showCommands(name.slice(1),true);
  else if (!/\s+\S/.test(text)) showCommands("commands",true);
  else closeCommands();
  renderTarget();
}
async function sendCommand(command, fromDraft) {
  if (state.sending || state.roomRefreshing || !state.detail) {
    return {ok: false, message: "This conversation is still sending, reconnecting, or loading. Try again when it finishes."};
  }
  const name = commandName(command);
  if (name === "/new" && ((!fromDraft && $("#draft").value.trim()) || currentUploads().length)) {
    setReceipt("Your current draft is kept. Send or clear it before starting another chapter.", "warn");
    return {ok: false, message: "Your current draft is kept. Send or clear it before starting another chapter."};
  }
  if (["/model","/effort"].includes(name) && commandSupported(name) && !/\s+\S/.test(command.trim())) {showCommands(name.slice(1),fromDraft);return;}
  if (name === "/steer" && !/\s+\S/.test(command.trim())) {setReceipt("Use /steer followed by your direction for the running turn.","warn");return;}
  const roomId = state.room, seq = state.roomSeq, threadId = (state.detail.native || {}).thread_id;
  const sourceDesktop = selectedLiveDesktopId();
  const sourceLocalOnly = deviceOnlyLayout();
  const sourceTab = findTab(agentId(), roomId) ? {...findTab(agentId(), roomId)} : null;
  const commandAgent = agentId(), commandGen = state.agentGen;
  const stillCurrent = () => commandAgent === agentId() && commandGen === state.agentGen && !stale(seq,roomId);
  const snapshot = $("#draft").value;
  const key = agentKey("atlas.commandAttempt." + roomId);
  let attempt = null;
  try {attempt = JSON.parse(localStorage.getItem(key));} catch(e) {}
  if (!attempt || attempt.command !== command || attempt.thread_id !== threadId) attempt = {client_id:clientId(),command,thread_id:threadId};
  try {localStorage.setItem(key,JSON.stringify(attempt));} catch(e) {}
  state.sending = true; clearTimeout(draftTimer); closeCommands(); renderTarget(); setSendState("Running " + name + "…","ok");
  try {
    const result = await api(agentPath(commandAgent, "/api/room/" + encodeURI(roomId) + "/command"), {absolute:true,method:"POST",body:attempt});
    const outcome = result.command || {}, sameRoom = stillCurrent();
    const refused = ["unsupported","failed","uncertain","needs_input","needs_idle","needs_sign_in"].includes(outcome.state);
    if (outcome.state !== "uncertain") {try {localStorage.removeItem(key);} catch(e) {}}
    if (refused) {
      if (sameRoom) setReceipt(commandMessage(outcome) || "The command did not complete. Your text is kept.","warn");
      else flash(commandMessage(outcome) || "The command did not complete.");
      return {ok: false, message: commandMessage(outcome) || "The command did not complete. Your text is kept."};
    }
    if (fromDraft && sameRoom) await settleDraftAfterSend(roomId,seq,snapshot.trim(),snapshot,sameRoom);
    if (sameRoom && result.room && result.room.id === roomId) state.detail = result.room;
    if (name === "/new" && (outcome.new_room || result.new_room || (result.room && result.room.id !== roomId))) {
      const next = outcome.new_room || result.new_room || result.room;
      const nextId = outcome.chapter
        ? await finishChapter(commandAgent, roomId, next, outcome.chapter, sourceDesktop, sourceTab, sourceLocalOnly)
        : (typeof next === "string" ? next : next.id);
      if (stillCurrent()) await openTabForTyping(commandAgent, nextId);
      void loadWorkspace(true);
    }
    if (sameRoom && name === "/goal" && (Object.hasOwn(outcome,"goal") || Object.hasOwn(outcome.native || {},"goal"))) {
      state.goalPanel = outcome.goal || (outcome.native || {}).goal || null;
      state.goalPanelTarget = {agent: agentId(), room: roomId};
      state.goalPanelStatus = {state: Object.hasOwn(outcome, "goal") || Object.hasOwn(outcome.native || {}, "goal") ? "known" : "unknown", goal: state.goalPanel};
      showCommands("goal",false);
    }
    if (sameRoom && name === "/help") {if (Array.isArray(outcome.help)) state.detail.commands=outcome.help.filter(entry=>!Array.isArray(outcome.supported)||outcome.supported.includes(entry.name)); showCommands("commands",false);}
    if (sameRoom && name === "/refresh") state.connectionRefreshState = ["refreshed","partial"].includes(outcome.state) ? outcome.state : "";
    const text = name === "/status" && outcome.native
      ? (outcome.native.model || "Model unavailable") + " · " + (outcome.native.reasoning_effort || "effort unavailable") + " · " + (outcome.native.active_turn || outcome.native.active_run ? "Running" : "No active turn")
      : commandMessage(outcome) || name + " completed";
    if (sameRoom && commandAgent === agentId()) setReceipt(text,["partial","unconfirmed"].includes(outcome.state) ? "warn" : "saved"); else flash(text);
    return {ok: true};
  } catch(error) {
    const known = error.status && error.status >= 400 && error.status < 500;
    if (known) {try {localStorage.removeItem(key);} catch(e) {}}
    const busy = error.code === "connection_busy";
    let text = busy ? name + " could not run yet. " + error.message
      : known ? error.message : "Could not confirm the command result. Your draft is kept; retry uses the same request ID.";
    let tone = busy ? "warn" : "fail";
    // Some native adapters report busy after the goal has already started.
    // Observe once; never retry the resume or attribute this state to it.
    if (busy && command.trim() === "/goal resume" && stillCurrent()) {
      try {
        const read = await api(agentPath(commandAgent, "/api/room/" + encodeURI(roomId) + "/command"),
          {absolute: true, method: "POST", body: {command: "/goal", client_id: clientId()}});
        if (!stillCurrent()) return;
        const outcome = read.command || {}, goal = outcome.goal || outcome.native?.goal;
        const room = read.room, activeTurn = room?.native?.active_turn;
        if (outcome.state === "ready" && goal?.status === "active" && room?.id === roomId
            && activeTurn && activeTurn !== room.native_terminal?.turn_id) {
          state.goalPanel = goal;
          state.goalPanelTarget = {agent: commandAgent, room: roomId};
          state.goalPanelStatus = {state: "known", goal};
          showCommands("goal", false);
          text = "Goal is active; this session is already working.";
          tone = "saved";
        }
      } catch (_) { /* A failed read leaves the original busy warning intact. */ }
      if (!stillCurrent()) return;
    }
    if (stillCurrent()) setReceipt(text,tone); else flash(text);
    return {ok: false, message: text};
  } finally {
    state.sending = false;
    if (stillCurrent()) {await refreshRoomState(); await refreshTail();}
    renderTarget(); renderActivity();
  }
}

$("#btnGoal").addEventListener("click", () => openGoalPanel());

/* ------------------------------------------------------------------ wiring */
let scrollPointer = false;
let touchY = null;
const readingStream = $("#stream");
function readingGesture(away) {
  if (away) state.following = false;
}
readingStream.addEventListener("wheel", event => readingGesture(event.deltaY < 0), {passive: true});
readingStream.addEventListener("touchstart", event => {
  touchY = event.touches[0]?.clientY ?? null;
}, {passive: true});
readingStream.addEventListener("touchmove", event => {
  const y = event.touches[0]?.clientY;
  if (touchY !== null && y !== undefined) readingGesture(y > touchY);
  touchY = y ?? null;
}, {passive: true});
readingStream.addEventListener("keydown", event => {
  if (["ArrowUp", "PageUp", "Home"].includes(event.key) || (event.key === " " && event.shiftKey)) readingGesture(true);
  else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) readingGesture(false);
});
readingStream.addEventListener("pointerdown", event => {
  // The scrollbar's target is the scroll container, not the transcript text.
  scrollPointer = event.pointerType === "mouse" && event.target === readingStream
    && event.clientX >= readingStream.getBoundingClientRect().right - 18;
  if (scrollPointer) readingGesture(true);
}, {passive: true});
window.addEventListener("pointerup", () => { scrollPointer = false; }, {passive: true});
readingStream.addEventListener("scroll", () => {
  const near = distanceFromBottom() < FOLLOW_PX;
  if (!state.sel && !state.anchor && near) state.following = true;
  else if (scrollPointer) state.following = false;
  if (near && state.newCount) { state.newCount = 0; updateNewPill(); }
  if (state.room) state.positions[state.room] = $("#stream").scrollTop;
}, {passive: true});

// Late image/font growth and keyboard/composer resizing keep a live reader
// at the tail, while leaving a reader of earlier messages in place.
const transcriptResize = new ResizeObserver(followLayout);
transcriptResize.observe($("#thread"));
transcriptResize.observe(readingStream);
$("#newJump").addEventListener("click", backToLive);
$("#selLive").addEventListener("click", backToLive);
$("#selPrev").addEventListener("click", () => state.sel && selectEntry(state.sel.kind, state.sel.index - 1));
$("#selNext").addEventListener("click", () => state.sel && selectEntry(state.sel.kind, state.sel.index + 1));

$("#draft").addEventListener("input", scheduleDraftSave);
$("#draft").addEventListener("input", commandDraftChanged);
$("#btnModel").addEventListener("click", () => {showCommands("model",false);$("#commandClose").focus();});
$("#commandClose").addEventListener("click", () => {closeCommands();$("#draft").focus();});
$("#commandPopover").addEventListener("keydown", event=>{if(event.key === "Escape"){event.preventDefault();closeCommands();$("#draft").focus();}});
$("#draft").addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#commandPopover").hidden) {event.preventDefault();closeCommands();return;}
  if (event.key === "ArrowDown" && !$("#commandPopover").hidden) {event.preventDefault();const choice=$("#commandOptions .command-choice");if(choice)choice.focus();return;}
  if (event.isComposing) return;
  if (event.key === "Enter" && !event.shiftKey && !event.metaKey && !event.ctrlKey) {
    event.preventDefault();
    send();
  }
});
$("#composer").addEventListener("submit", (event) => { event.preventDefault(); send(); });
$("#btnContinue").addEventListener("click", (event) => continueHere(event.currentTarget));

$("#navSearch").addEventListener("input", () => {
  const query = $("#navSearch").value.trim();
  renderNavList(query);
  clearTimeout(navSearchTimer);
  navSearchTimer = setTimeout(() => runNavSearch(query), 200);
});
$("#btnFold").addEventListener("click", toggleFold);
$("#btnEarlier").addEventListener("click", loadEarlier);
$("#btnFocus").addEventListener("click", () => toggleFocus());
$("#btnDetails").addEventListener("click", (event) => openPanel("details", event.currentTarget));

/* left drawer: navigation, projects and the command center */
for (const [id,view] of [["btnEmail","email"],["btnConstellation","constellation"],["btnSchedule","schedule"],["btnUsage","usage"]]) {
  $("#"+id).addEventListener("click", () => { showView(view); dismissOverlay(); });
}
$("#btnConsole").addEventListener("click", () => { showView("console"); dismissOverlay(); });
$("#btnBoard").addEventListener("click", () => {
  showView($("#viewBoard").hidden ? "board" : "console");
  dismissOverlay();
});
$("#btnTell").addEventListener("click", () => {
  showView($("#viewTell").hidden ? "tell" : "console");
  dismissOverlay();
});
$("#btnLeftToggle").addEventListener("click", (event) => toggleLeft(event.currentTarget));
$("#btnMenu").addEventListener("click", (event) => toggleLeft(event.currentTarget));
$("#btnRailBrowse").addEventListener("click", () => {
  setLeft("wide");
  setTimeout(() => $("#roomSearch").focus(), 0);
});
$("#roomSearch").addEventListener("input", () => {
  clearTimeout(roomSearchTimer);
  roomSearchTimer = setTimeout(renderRoomList, 180);
});
$("#fltWorkers").addEventListener("change", renderRoomList);
$("#fltComplete").addEventListener("change", renderRoomList);

/* right drawer */
$("#btnDock").addEventListener("click", (event) => {
  if (state.ui.dock && ($("#app").dataset.dock !== "closed")) { closeDock(true); return; }
  openPanel(state.ui.dock || "turns", event.currentTarget);
});
$("#dockClose").addEventListener("click", () => closeDock(true));

/* Desktops, the tab menu and dragging. None of these reach a runtime. */
$("#btnDesktops").addEventListener("click", () => openDesktopDialog());
/* The mark. It opens one conversation, the ordinary way, and leaves the rest
   of the strip alone. */
$("#btnYourUx").addEventListener("click", (event) => openNewConversation(event.currentTarget));
/* ------------------------------------------------------ new conversation */
$("#btnNewConv").addEventListener("click", (event) => openNewConversation(event.currentTarget));
$("#btnNewConvPhone").addEventListener("click", (event) => {
  openNewConversation(event.currentTarget);
});
$("#newClose").addEventListener("click", () => closeNewConversation());
$("#newCancel").addEventListener("click", () => closeNewConversation());
$("#newReset").addEventListener("click", () => resetNewConversation());
$("#newRetry").addEventListener("click", () => { void submitNewConversation(); });
$("#newSearch").addEventListener("input", () => renderNewWhere());
$("#newName").addEventListener("input", (event) => {
  newConv.title = event.currentTarget.value;
});
$("#newForm").addEventListener("submit", (event) => {
  event.preventDefault();
  // A second click while the first is in flight is the same click.
  if (newConv.phase !== "ready") return;
  void submitNewConversation();
});
// Escape, the backdrop and the phone's Back button all mean the same thing.
$("#newDialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  if (newConv.phase === "pending") return;      // do not walk out mid-create
  closeNewConversation();
});
$("#newDialog").addEventListener("click", (event) => {
  if (event.target === $("#newDialog") && newConv.phase !== "pending") closeNewConversation();
});
window.addEventListener("popstate", () => {
  if (newConv.open) closeNewConversation(true);
});

$("#deskClose").addEventListener("click", () => $("#deskDialog").close());
$("#deskLocalOnly").addEventListener("change", (event) => {
  void setLayoutScope(event.currentTarget.checked);
});
/* A deferred change of conversation is exactly what a person leaving the
   composer alone was waiting for. */
$("#draft").addEventListener("blur", () => { void followDeferredSession(); });
$("#deskSave").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("#deskName").value;
  const result = await saveDesktop(name);
  if (result && result.ok) $("#deskDialog").close();
});
$('#deskNewLive').addEventListener('submit',async event=>{event.preventDefault();
  const result=await createLiveDesktop($('#deskNewName').value,null,$('#deskNewDescription').value);
  if(result.ok){desktopChooser.selected=selectedLiveDesktopId();desktopChooser.manageRendered=false;$('#deskNewName').value=nextDesktopName();$('#deskNewDescription').value='';renderDesktopDialog();void checkDesktopDevice(true);}
});
$('#deskManageToggle').addEventListener('click',showDesktopManagement);
$('#deskDialog').addEventListener('cancel',event=>{if(desktopChooser.busy)event.preventDefault();});
$('#deskCancel').addEventListener('click',()=>$('#deskDialog').close());
$('#deskApply').addEventListener('click',()=>void applyDesktopChoice());
$('#deskLive').addEventListener('keydown',event=>{
  if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;
  const cards=[...$('#deskLive').querySelectorAll('[role=radio]:not(:disabled)')],index=cards.indexOf(document.activeElement);if(index<0)return;
  event.preventDefault();event.stopPropagation();const next=(index+(['ArrowRight','ArrowDown'].includes(event.key)?1:-1)+cards.length)%cards.length;
  cards[next].click();$('#deskLive').querySelectorAll('[role=radio]')[next]?.focus();
});
document.addEventListener("pointermove", tabPointerMove, {passive: false});
document.addEventListener("pointerup", () => endTabDrag(true));
document.addEventListener("pointercancel", () => endTabDrag(false));
document.addEventListener("pointerdown", (event) => {
  if (!$("#tabMenu").hidden && !$("#tabMenu").contains(event.target)
      && !event.target.closest(".tmenu")) closeTabMenu(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (tabDrag) { endTabDrag(false); return; }
  if (!$("#tabMenu").hidden) { event.stopPropagation(); closeTabMenu(true); }
}, true);
$("#btnAttention").addEventListener("click", (event) => {
  if (state.ui.dock === "attention" && $("#app").dataset.dock !== "closed") { closeDock(true); return; }
  openPanel("attention", event.currentTarget);
});
$("#attnSort").addEventListener("click", () => {
  setAttentionOrder(state.attn.order === "urgency" ? "project" : "urgency");
});
$("#attnCommandCenter").addEventListener("click", () => {
  // The legacy command center is still the full record; this is the way in.
  showView("board");
  dismissOverlay();
});
for (const tab of document.querySelectorAll(".dock-tab")) {
  tab.addEventListener("click", () => openPanel(tab.dataset.panel, tab));
}

$("#voiceSelect").addEventListener("change", (event) => {
  state.voice.preferred = event.target.value;
  try { window.localStorage.setItem("atlas.voice", state.voice.preferred); } catch (e) { /* ignore */ }
  clearSpeech();
  renderStream();
});
$("#moreFold").addEventListener("click", () => { toggleFold(); renderReadingControls(); dismissOverlay(); });
$("#moreEarlier").addEventListener("click", () => { loadEarlier(); renderReadingControls(); dismissOverlay(); });
$("#moreFocus").addEventListener("click", () => { toggleFocus(); renderReadingControls(); dismissOverlay(); });

$("#scrim").addEventListener("click", () => closeOverlay(true));
$("#btnFocusExit").addEventListener("click", () => toggleFocus(false));

// Workspace controls stay at the foot of navigation, independent of its list.
const workspaceMenu = $("#workspaceMenu");
function placeWorkspaceMenu() {
  const anchor = $("#btnWorkspaceSettings").getBoundingClientRect();
  workspaceMenu.style.left = Math.max(8, Math.min(anchor.left,
    window.innerWidth - 254 - 8)) + "px";
  workspaceMenu.style.bottom = Math.max(8, window.innerHeight - anchor.top + 8) + "px";
  workspaceMenu.style.maxHeight = Math.max(80, anchor.top - 16) + "px";
}
workspaceMenu.addEventListener("beforetoggle", (event) => {
  if (event.newState !== "open") return;
  $("#settingsAgent").textContent = agentLabel();
  placeWorkspaceMenu();
});
window.addEventListener("resize", () => {
  if (workspaceMenu.matches(":popover-open")) placeWorkspaceMenu();
});
workspaceMenu.addEventListener("click", (event) => {
  const button = event.target.closest("[data-settings]");
  if (!button) return;
  workspaceMenu.hidePopover();
  if (["efficiency", "skills"].includes(button.dataset.settings)) {
    dismissOverlay(); void openEfficiency(button.dataset.settings === "skills" ? "skills" : "usage"); return;
  }
  if (button.dataset.settings === "desktops") {
    dismissOverlay();
    openDesktopDialog();
    return;
  }
  openRoomSettings();
  if (button.dataset.settings === "room") return;
  const target = button.dataset.settings === "appearance"
    ? $("#themePick button") : $("#connDetails");
  setTimeout(() => {
    target.scrollIntoView({block: "nearest"});
    target.focus({preventScroll: true});
  }, 0);
});

/* Escape leaves focus — but only when nothing nearer has already claimed it.
   A dialog, a menu, a popover or an open drawer all answer first, because the
   thing on top is the thing Escape is about. */
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !state.ui.focus || event.defaultPrevented) return;
  if (workspaceMenu.matches(":popover-open")) return;
  if (document.querySelector("dialog[open]")) return;
  if (state.ui.overlay) return;
  for (const id of ["#tabMenu", "#commandPopover", "#plusMenu", "#uploadMenu"]) {
    const node = $(id);
    if (node && !node.hidden) return;
  }
  event.preventDefault();
  toggleFocus(false);
});

/* A drawer over the console is modal: Escape closes it and Tab stays inside.
   A docked drawer beside the console is not, so neither applies there. */
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (workspaceMenu.matches(":popover-open")) return;
  if (state.ui.overlay) { event.preventDefault(); closeOverlay(true); return; }
  if (isWide() && state.ui.dock && document.activeElement
      && $("#dock").contains(document.activeElement)) {
    event.preventDefault();
    closeDock(true);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  if (workspaceMenu.matches(":popover-open")) return;
  const node = overlayNode();
  if (!node) return;
  const focusable = [...node.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])')]
    .filter((n) => !n.disabled && n.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  else if (!node.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
});

/* Crossing the docked/overlay threshold must not leave a half-open shell. */
let shellTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(shellTimer);
  shellTimer = setTimeout(applyShell, 120);
});

/* ------------------------------------------------------------- the field
   The composer grows with what is being written, up to the cap the stylesheet
   sets, so a long message is visible without turning the field into a page. */
function resizeDraft() {
  const box = $("#draft");
  if (!box) return;
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, parseInt(
    getComputedStyle(box).maxHeight, 10) || 140) + "px";
}
$("#draft").addEventListener("input", resizeDraft);

/* --------------------------------------------------------- phone viewport
   An on-screen keyboard shortens the window without telling CSS. Reading the
   visual viewport keeps the app exactly as tall as what is actually visible,
   so the composer sits on the keyboard rather than under it, and marks the
   keyboard as open so the heading can give its second line back to the
   conversation. Safe areas are handled in the stylesheet. */
let viewportFrame = 0;
function updateViewport() {
  cancelAnimationFrame(viewportFrame);
  viewportFrame = requestAnimationFrame(() => {
    const vv = window.visualViewport;
    const typing = document.activeElement
      && document.activeElement.matches("textarea, input:not([type=checkbox])");
    const shrunk = vv ? window.innerHeight - vv.height : 0;
    document.body.classList.toggle("keyboard-open",
      Boolean(window.innerWidth <= 950 && typing && shrunk > 100));
    // A pinch-zoomed viewport is not a shorter window; leave the app alone.
    const settled = vv && Math.abs(vv.scale - 1) < 0.01;
    const root = document.documentElement;
    root.style.setProperty("--vvh", (settled ? vv.height : window.innerHeight) + "px");
    root.style.setProperty("--vvtop", (settled ? vv.offsetTop : 0) + "px");
    resizeDraft();
  });
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", updateViewport);
  window.visualViewport.addEventListener("scroll", updateViewport);
}
window.addEventListener("resize", updateViewport);
$("#draft").addEventListener("focus", updateViewport);
$("#draft").addEventListener("blur", updateViewport);
updateViewport();

/* ------------------------------------------------------------- appearance
   Dark by default, with the UX46 light palette available and a system option
   that follows the device. The choice is this browser's, not the session's. */
const THEME_KEY = "atlas.theme";
const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
function applyTheme() {
  const want = state.theme || "dark";
  const resolved = want === "system" ? (darkQuery && !darkQuery.matches ? "light" : "dark") : want;
  if (resolved === "light") document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  for (const button of document.querySelectorAll("#themePick button")) {
    button.setAttribute("aria-pressed", String(button.dataset.theme === want));
  }
}
function setTheme(choice) {
  state.theme = choice;
  savePref(THEME_KEY, choice === "dark" ? "" : choice);
  applyTheme();
  publishCustomizations();
}
for (const button of document.querySelectorAll("#themePick button")) {
  button.addEventListener("click", () => setTheme(button.dataset.theme));
}
if (darkQuery && darkQuery.addEventListener) {
  darkQuery.addEventListener("change", () => { if (state.theme === "system") applyTheme(); });
}
state.theme = readPref(THEME_KEY) || "dark";
applyTheme();

/* ------------------------------------------------------------ attachments
   Everything a message can carry sits behind the composer's +, so the field
   itself stays one line on a phone. A file is uploaded to the console's own
   store, which hands back opaque urls; the message that carries it goes
   through the durable queue, exactly as before.

   An attachment belongs to the room it was chosen in. Switching sessions
   while one is still uploading must never move a file into another
   conversation, so every entry carries its own room and every request is
   addressed to that room rather than to whichever one is open when the
   response lands. File bytes never reach localStorage; only the record the
   console minted does, so a reload still knows what was chosen. */
let uploadDrafts = new Map();
const uploadsByAgent = new Map();
const UPLOAD_DRAFT_KEY = "ux46.upload-drafts.1";
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

function uploadBuckets(owner = agentId()) {
  if (uploadsByAgent.has(owner)) return uploadsByAgent.get(owner);
  const buckets = new Map();
  try {
    const key = UPLOAD_DRAFT_KEY + (owner === DEFAULT_AGENT ? "" : "@" + owner);
    const saved = JSON.parse(localStorage.getItem(key) || "{}");
    for (const [room, entries] of Object.entries(saved)) {
      if (Array.isArray(entries)) buckets.set(room, entries.filter(e => e && e.file && e.file.id));
    }
  } catch (_) { /* Selected files remain available in this page. */ }
  uploadsByAgent.set(owner, buckets);
  return buckets;
}
function loadUploadDrafts() { uploadDrafts = uploadBuckets(); }
loadUploadDrafts();

function saveUploadDrafts(owner = agentId()) {
  try {
    const key = UPLOAD_DRAFT_KEY + (owner === DEFAULT_AGENT ? "" : "@" + owner);
    localStorage.setItem(key, JSON.stringify(Object.fromEntries(
      [...uploadBuckets(owner)].map(([room, entries]) => [room, entries.filter(e => e.file).map(
        e => ({name: e.name, size: e.size, file: e.file, status: e.status, attempt: e.attempt, agent: owner}))])
    )));
  } catch (_) { /* The central file remains stored when local storage is unavailable. */ }
}

const NO_UPLOADS = [];
function currentUploads(room = state.room, owner = agentId()) {
  if (!room) return NO_UPLOADS;
  const buckets = uploadBuckets(owner);
  if (!buckets.has(room)) buckets.set(room, []);
  return buckets.get(room);
}

function fileSize(bytes) {
  const size = Number(bytes || 0);
  if (!size) return "";
  if (size < 1024) return size + " B";
  if (size < 1024 * 1024) return (size / 1024).toFixed(0) + " KB";
  return (size / (1024 * 1024)).toFixed(1) + " MB";
}

/* What a chip says about itself. "chosen here" is deliberate: until the
   console answers, the file exists only in this browser. */
function chipNote(entry) {
  const size = fileSize(entry.size);
  const with_ = (words) => [size, words].filter(Boolean).join(" · ");
  switch (entry.status) {
    case "uploading": return with_("uploading…");
    case "ready": return with_("ready to send");
    case "failed": return entry.error ? "upload failed · " + entry.error : "upload failed";
    case "sending": return with_("sending…");
    case "queued": return with_("queued");
    case "unconfirmed": return with_("delivery unconfirmed");
    case "notsent": return with_("not sent");
    case "chosen": return with_("chosen here, not uploaded yet");
    default: return with_(entry.status || "");
  }
}
const CHIP_TONE = {failed: "warn", notsent: "warn", unconfirmed: "warn",
                   ready: "ready", uploading: "busy", chosen: "busy", sending: "busy"};

/* A server-minted artifact URL, either this console's own or one it rewrote
   from a remote agent onto this same origin. Anything else is not rendered. */
function managedUploadUrl(value) {
  return /^(\/api\/agents\/[a-z][a-z0-9-]{0,31})?\/api\/atlas\/files\/[A-Za-z0-9_-]+\/(preview|download)(\?[^\s]*)?$/.test(String(value || "")) ? value : "";
}

const composerForm = $("#composer");
const quickDraft = $("#draft");
const uploadChips = $("#cchips");
const uploadPlus = $("#btnPlus");
const uploadMenu = $("#plusMenu");
const uploadOptions = $("#plusOptions");

function renderUploadChips() {
  const entries = currentUploads();
  uploadChips.replaceChildren();
  for (const entry of entries) {
    const chip = el("span", {class: "fchip " + (CHIP_TONE[entry.status] || "")});
    const preview = managedUploadUrl(entry.file && entry.file.preview_url);
    if (preview && String(entry.file.mime).startsWith("image/")) {
      chip.append(el("img", {src: preview, alt: ""}));
    }
    chip.append(el("span", {class: "chip-text"}, [
      el("b", {text: entry.name, title: entry.name}),
      el("span", {class: "sz", text: chipNote(entry)}),
    ]));
    if (entry.file && managedUploadUrl(entry.file.download_url)) {
      chip.append(el("a", {class: "dl", href: entry.file.download_url, download: entry.name,
        title: "Download " + entry.name, "aria-label": "Download " + entry.name},
        [replyIcon("download")]));
    }
    if (entry.status === "failed" && entry.blob) {
      chip.append(el("button", {class: "retry", type: "button", text: "Retry",
        "aria-label": "Retry uploading " + entry.name, on: {click: () => uploadOne(entry)}}));
    }
    if (entry.attempt) {
      chip.append(el("button", {class: "retry", type: "button", text: "Check",
        "aria-label": "Check delivery of " + entry.name,
        on: {click: () => checkUploadMessage(entry.attempt, state.room)}}));
    } else {
      chip.append(el("button", {class: "x closebtn", type: "button", title: "Remove " + entry.name,
        "aria-label": "Remove " + entry.name,
        on: {click: () => {
          uploadDrafts.set(state.room, currentUploads().filter(e => e !== entry));
          saveUploadDrafts(); renderUploadChips(); reportDraftState();
        }}}, [useIcon("i-close")]));
    }
    uploadChips.append(chip);
  }
  // Files chosen in another session are neither lost nor silently sent here.
  let elsewhere = 0;
  for (const [room, list] of uploadDrafts) if (room !== state.room) elsewhere += list.length;
  if (elsewhere) {
    uploadChips.append(el("span", {class: "fchip note",
      text: elsewhere === 1 ? "1 file is held for another session"
        : elsewhere + " files are held for other sessions"}));
  }
  if (uploadPlus) uploadPlus.disabled = !state.room
    || !(state.detail && state.detail.controllable);
}

function closeUploadMenu() {
  uploadMenu.hidden = true;
  uploadPlus.setAttribute("aria-expanded", "false");
}

function chooseUpload(label, hint, accept, capture) {
  const input = el("input", {type: "file", hidden: true, accept, multiple: !capture, capture});
  const button = el("button", {class: "command-choice", type: "button",
    on: {click: () => { input.dataset.room = state.room || ""; input.dataset.agent = agentId(); closeUploadMenu(); input.click(); }}},
    [el("b", {text: label}), el("span", {text: hint})]);
  input.addEventListener("change", () => {
    const room = input.dataset.room;
    const files = [...input.files];
    input.value = "";
    if (room) uploadChosenFiles(files, room, input.dataset.agent);
  });
  uploadOptions.append(button, input);
}
chooseUpload("Photos", "from this device", "image/*");
// A camera is only offered where one is plausibly attached to the finger.
if (window.matchMedia && window.matchMedia("(pointer:coarse)").matches) {
  chooseUpload("Take photo", "use the camera", "image/*", "environment");
}
chooseUpload("Files", "any file up to 20 MB", null);
uploadOptions.append(el("p", {class: "command-note",
  text: "Enter sends · Shift+Enter starts a line. A file travels with the message you send next."}));

uploadPlus.addEventListener("click", () => {
  uploadMenu.hidden = !uploadMenu.hidden;
  uploadPlus.setAttribute("aria-expanded", String(!uploadMenu.hidden));
  if (!uploadMenu.hidden) {
    const first = uploadOptions.querySelector("button");
    if (first) setTimeout(() => first.focus(), 0);
  }
});
$("#plusClose").addEventListener("click", () => { closeUploadMenu(); quickDraft.focus(); });
document.addEventListener("click", (event) => {
  if (!composerForm.contains(event.target)) closeUploadMenu();
});
composerForm.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !uploadMenu.hidden) {
    event.preventDefault(); closeUploadMenu(); uploadPlus.focus();
  }
});

async function uploadChosenFiles(files, room, owner = agentId()) {
  if (!room) { setSendState("open a session before attaching a file", "warn"); return; }
  const bucket = currentUploads(room, owner);
  const chosen = [...files].slice(0, 8);
  const entries = chosen.map((blob) => {
    const entry = {name: blob.name || "file", size: blob.size, blob, agent: owner, room,
                   status: "chosen", file: null, error: "", attempt: null};
    bucket.push(entry);
    return entry;
  });
  if (room === state.room && owner === agentId()) { renderUploadChips(); reportDraftState(); }
  // One at a time: a phone on a slow link should not race itself, and the
  // composer can say exactly which file it is still waiting for.
  for (const entry of entries) await uploadOne(entry, room);
}

async function uploadOne(entry, room) {
  const target = room || entry.room || [...uploadDrafts].find(([, list]) => list.includes(entry))?.[0] || state.room;
  const owner = entry.agent || agentId();
  const mine = () => state.room === target && agentId() === owner;
  entry.status = "uploading";
  entry.error = "";
  if (mine()) { renderUploadChips(); reportDraftState(); }
  try {
    if (!entry.blob) throw new Error("this browser no longer holds the file — choose it again");
    if (entry.size > MAX_UPLOAD_BYTES) throw new Error("over the 20 MB limit");
    const data = new FormData();
    data.append("file", entry.blob, entry.name);
    // The room is an argument, never state.room, so a mid-flight session
    // switch cannot redirect a file into another conversation.
    const response = await consoleFetch(agentPath(owner, "/api/room/" + encodeURI(target) + "/files"), {
      method: "POST", body: data,
      headers: {"X-Atlas-CSRF": state.csrf, "Accept": "application/json"},
    });
    let payload = null;
    try { payload = await response.json(); } catch (e) { payload = null; }
    if (!response.ok || !payload || !payload.file || !payload.file.id) {
      throw new Error((payload && payload.message) || "the upload was refused (" + response.status + ")");
    }
    entry.file = payload.file;
    entry.name = payload.file.name || entry.name;
    entry.size = payload.file.size || entry.size;
    entry.status = "ready";
  } catch (error) {
    entry.file = null;
    entry.status = "failed";
    entry.error = String((error && error.message) || error);
  }
  saveUploadDrafts(owner);
  if (mine()) { renderUploadChips(); reportDraftState(); }
  return entry.status === "ready";
}

quickDraft.addEventListener("paste", (event) => {
  const files = [...(event.clipboardData?.files || [])];
  if (files.length && state.room) { event.preventDefault(); uploadChosenFiles(files, state.room); }
});
function draggingFiles(event) {
  const transfer = event.dataTransfer;
  return Array.from(transfer?.types || []).includes("Files")
    || Array.from(transfer?.items || []).some(item => item.kind === "file");
}
document.addEventListener("dragover", event => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  if (!$("#viewConsole").hidden && state.room) $("#viewConsole").dataset.drop = "1";
});
document.addEventListener("dragleave", event => {
  if (!event.relatedTarget) delete $("#viewConsole").dataset.drop;
});
document.addEventListener("drop", event => {
  delete $("#viewConsole").dataset.drop;
  if (!draggingFiles(event)) return;
  event.preventDefault();
  const files = Array.from(event.dataTransfer?.files || []);
  if (!files.length) {
    for (const item of Array.from(event.dataTransfer?.items || [])) {
      if (item.kind === "file") { const file = item.getAsFile(); if (file) files.push(file); }
    }
  }
  if (!state.room || $("#viewConsole").hidden) {
    flash("Open a conversation, then drop the screenshot there.");
    return;
  }
  if (!files.length) { flash("The screenshot could not be read. Use + to choose the file."); return; }
  void uploadChosenFiles(files, state.room, agentId());
});
window.addEventListener("blur", () => { delete $("#viewConsole").dataset.drop; });

const renderTargetWithoutUploads = renderTarget;
renderTarget = function () { renderTargetWithoutUploads(); renderUploadChips(); };

async function checkUploadMessage(id, room, agent) {
  const owner = agent || agentId();
  try {
    // Asked of the agent that queued it, by name: another agent has never
    // heard of this id and would answer as though nothing were queued.
    const data = await api(agentPath(owner, "/api/room/" + encodeURI(room) + "/pending"),
                           {absolute: true});
    const item = (data.pending || []).find(p => p.client_id === id);
    if (!item) {
      if (room === state.room) setReceipt("Delivery not confirmed. Files retained; nothing resent.", "warn");
      return;
    }
    if (item.status === "accepted") {
      uploadDrafts.set(room, currentUploads(room).filter(e => e.attempt !== id));
      saveUploadDrafts();
      if (room === state.room) { setReceipt("Message and files accepted", "saved"); await refreshTail(); }
    } else {
      const next = {pending: "queued", dispatching: "sending", failed: "notsent"}[item.status]
        || "unconfirmed";
      for (const entry of currentUploads(room).filter(e => e.attempt === id)) entry.status = next;
      saveUploadDrafts();
      if (room === state.room) {
        setReceipt(item.status === "pending" ? "Message and files queued"
          : item.status === "dispatching" ? "Sending message and files…"
          : item.queued_reason || "Check delivery before sending again", "warn");
      }
    }
    renderUploadChips();
    return item.status;
  } catch (error) {
    if (room === state.room) setReceipt("Cannot check delivery yet. Nothing resent.", "warn");
  }
}

const sendWithoutUploads = send;
send = async function () {
  const entries = currentUploads().slice();
  if (!entries.length) return sendWithoutUploads();
  if (state.sending) return;
  if (entries.some(e => e.attempt)) {
    setReceipt("Check the earlier file message before sending again.", "warn"); return;
  }
  const notReady = entries.filter(e => e.status !== "ready");
  if (notReady.length) {
    setReceipt(notReady.some(e => e.status === "failed")
      ? "Retry or remove the files that did not upload."
      : "Wait for the uploads to finish.", "warn");
    return;
  }
  if (state.roomRefreshing || state.detail?.ownership?.state !== "atlas_owned") {
    setReceipt("Connect to this session before sending files.", "warn"); return;
  }
  if (quickDraft.value.trim().startsWith("/")) {
    setReceipt("Send files with a message; use slash commands separately.", "warn"); return;
  }
  const room = state.room, seq = state.roomSeq, snapshot = quickDraft.value;
  const owner = agentId();
  const body = snapshot.trim() || neutralAttachmentBody(entries);
  const id = clientId();
  entries.forEach(e => { e.attempt = id; e.status = "sending"; });
  saveUploadDrafts();
  state.sending = true; renderTarget(); renderActivity(); setReceipt("Sending message and files…", "ok");
  try {
    await api("/api/room/" + encodeURI(room) + "/pending", {method: "POST", body: {
      client_id: id, thread_id: state.detail.native.thread_id, body,
      attachments: entries.map(e => ({file_id: e.file.id})),
    }});
    await settleDraftAfterSend(room, seq, snapshot.trim(), snapshot, !stale(seq, room));
    entries.forEach(e => e.status = "queued");
    saveUploadDrafts();
    if (!stale(seq, room)) setReceipt("Message and files queued", "saved");
    await loadPending(room, seq);
    if (!stale(seq, room)) { redrawKeepingPlace(); if (state.following) toTail(); }
    schedulePendingPoll();
    // The poll belongs to the agent that queued the message: switching agents
    // must not let it read another agent's queue or rewrite its saved chips.
    const gen = state.agentGen;
    (async () => {
      for (let i = 0; i < 20; i++) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        if (gen !== state.agentGen) return;
        const status = await checkUploadMessage(id, room, owner);
        if (status && !["pending", "dispatching"].includes(status)) break;
      }
    })();
  } catch (error) {
    if (error.status && error.status >= 400 && error.status < 500 && error.code !== "duplicate_mismatch") {
      entries.forEach(e => { e.attempt = null; e.status = "ready"; });
      if (!stale(seq, room)) setReceipt("Not sent: " + error.message, "fail");
    } else {
      entries.forEach(e => e.status = "unconfirmed");
      if (!stale(seq, room)) setReceipt("Delivery unconfirmed. Use Check; nothing will be resent.", "warn");
    }
    saveUploadDrafts();
  } finally { state.sending = false; renderTarget(); renderActivity(); }
};

/* The native queue will not take a message with no words. An attachment on
   its own therefore travels with a plain description of itself, written here
   and visible in the transcript — never an empty message, and never a file
   that is quietly dropped. */
function neutralAttachmentBody(entries) {
  const names = entries.map(e => (e.file && e.file.name) || e.name).filter(Boolean);
  if (!names.length) return "Attached a file.";
  if (names.length === 1) return "Attached " + names[0] + ".";
  return "Attached " + names.length + " files: " + names.join(", ") + ".";
}

/* --------------------------------------------------------- queued messages
   A message UX46 queued stays in the transcript, in the place it was
   written, until the native session takes it. It is editable while the queue
   still allows it and cancellable until it dispatches. Nothing here resends
   anything, and an accepted entry gives way to the native item rather than
   being duplicated beside it. */

const PENDING_ACTIVE = new Set(["pending", "editing", "dispatching"]);
/* A settled entry stays visible only long enough to be read; after that the
   native item, or the failure the person already saw, is the record. */
const PENDING_SETTLED_MS = 10 * 60 * 1000;
let pendingTimer = null;

function pendingPath(roomId, clientId) {
  return "/api/room/" + encodeURI(roomId) + "/pending"
    + (clientId ? "/" + encodeURIComponent(clientId) : "");
}

function putPending(entry) {
  if (!entry || !entry.client_id) return;
  state.pending = state.pending.filter((p) => p.client_id !== entry.client_id).concat(entry);
}

/* What belongs in this room's transcript right now. */
function pendingShown() {
  const known = new Set(state.items.map((i) => i.client_id).filter(Boolean));
  const now = Date.now();
  return state.pending
    .filter((p) => {
      if (p.room !== state.room) return false;
      if (known.has(p.client_id)) return false;      // the native item is here already
      if (state.pendingDismissed.has(p.client_id)) return false;
      if (p.status === "cancelled") return false;
      if (PENDING_ACTIVE.has(p.status)) return true;
      return now - Number(p.updated_at || 0) * 1000 < PENDING_SETTLED_MS;
    })
    .sort((a, b) => (a.position || 0) - (b.position || 0)
      || (a.created_at || 0) - (b.created_at || 0));
}

async function loadPending(roomId, seq) {
  if (!roomId || state.pendingSupport === "missing") return;
  seq = seq === undefined ? state.roomSeq : seq;
  try {
    const payload = await api(pendingPath(roomId));
    if (stale(seq, roomId)) return;
    state.pendingSupport = "ok";
    state.pending = state.pending.filter((p) => p.room !== roomId).concat(payload.pending || []);
  } catch (error) {
    if (error.status === 404) state.pendingSupport = "missing";
    // Any other failure leaves what is already known on screen: an unread
    // queue is not an empty one.
  }
}

function schedulePendingPoll() {
  clearTimeout(pendingTimer);
  if (!state.room || state.pendingSupport === "missing") return;
  const live = state.pending.some((p) => p.room === state.room && PENDING_ACTIVE.has(p.status));
  if (!live) return;
  const roomId = state.room;
  const seq = state.roomSeq;
  pendingTimer = setTimeout(async () => {
    await loadPending(roomId, seq);
    if (stale(seq, roomId)) return;
    redrawKeepingPlace();
    schedulePendingPoll();
  }, 900);
}

/* One queued message, said as what it is: not yet delivered. */
function pendingNode(item) {
  const editing = state.pendingEdit && state.pendingEdit.client_id === item.client_id;
  const settled = !PENDING_ACTIVE.has(item.status);
  const accepted = item.status === "accepted";
  const node = el("article", {class: "msg human" + (accepted ? "" : " pending"),
                              data: {cid: item.client_id}});
  const head = el("div", {class: "head"}, [el("span", {class: "who", text: HUMAN_NAME})]);
  head.appendChild(pendingTag(item, editing));
  node.appendChild(head);

  if (editing) {
    const box = el("textarea", {
      class: "pendedit", "aria-label": "Edit this queued message",
      on: {
        input: (event) => { state.pendingEdit.text = event.currentTarget.value; },
        keydown: (event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            savePendingEdit(item);
          }
          if (event.key === "Escape") { event.preventDefault(); stopPendingEdit(item); }
        },
      },
    });
    box.value = state.pendingEdit.text;
    node.appendChild(box);
    node.appendChild(el("div", {class: "pendacts"}, [
      el("button", {class: "linkbtn", type: "button", text: "Save and send",
        on: {click: () => savePendingEdit(item)}}),
      el("button", {class: "ghost", type: "button", text: "Keep as it was",
        on: {click: () => stopPendingEdit(item)}}),
      el("button", {class: "ghost", type: "button", text: "Cancel this message",
        on: {click: () => cancelPending(item)}}),
    ]));
    return node;
  }

  node.appendChild(el("div", {class: "body", text: item.body || ""}));
  const ids = (item.attachments || [])
    .map((a) => String((a && a.file_id) || ""))
    .filter((id) => ATTACHMENT_FILE_ID.test(id));
  if (ids.length) node.appendChild(attachmentStrip(ids));
  if (item.queued_reason) node.appendChild(el("p", {class: "pendwhy", text: item.queued_reason}));
  if (item.status === "uncertain") {
    node.appendChild(el("p", {class: "pendwhy",
      text: "UX46 will not send this again on its own. Check the session before rewriting it."}));
  }

  const acts = el("div", {class: "pendacts"});
  if (item.status === "pending") {
    acts.appendChild(el("button", {class: "linkbtn", type: "button", text: "Edit",
      on: {click: () => beginPendingEdit(item)}}));
    acts.appendChild(el("button", {class: "ghost", type: "button", text: "Cancel",
      on: {click: () => cancelPending(item)}}));
  } else if (settled && !accepted) {
    acts.appendChild(el("button", {class: "linkbtn", type: "button", text: "Put back in the composer",
      on: {click: () => restorePending(item)}}));
    acts.appendChild(el("button", {class: "ghost", type: "button", text: "Hide",
      on: {click: () => { state.pendingDismissed.add(item.client_id); redrawKeepingPlace(); }}}));
  }
  if (acts.childNodes.length) node.appendChild(acts);
  return node;
}

function pendingTag(item, editing) {
  if (editing) return el("span", {class: "pendtag warn", text: "Editing · held"});
  if (item.status === "dispatching") {
    return el("span", {class: "pendtag"}, [
      el("span", {class: "spin", "aria-hidden": "true"}),
      el("span", {text: "Sending"}),
    ]);
  }
  if (item.status === "accepted") return el("span", {class: "pendtag go", text: "Sent"});
  if (item.status === "failed") return el("span", {class: "pendtag fail", text: "Not sent"});
  if (item.status === "uncertain") return el("span", {class: "pendtag warn", text: "Delivery unknown"});
  if (item.status === "editing") return el("span", {class: "pendtag warn", text: item.queued_reason?.startsWith("Held after recovery") ? "Held after recovery" : "Held for editing"});
  return el("span", {class: "pendtag", text: "Pending"});
}

/* A 409 always carries the server's current row, so a disagreement ends with
   both sides looking at the same message. */
function pendingConflict(error, item) {
  if (error.detail && error.detail.client_id) putPending(error.detail);
  state.pendingEdit = null;
  redrawKeepingPlace();
  setReceipt(error.status === 409
    ? "that message moved on — " + (error.message || "it is no longer editable")
    : (error.message || "could not change that queued message"), "warn");
}

async function beginPendingEdit(item) {
  try {
    const result = await api(pendingPath(item.room, item.client_id), {
      method: "PATCH", body: {version: item.version, editing: true},
    });
    putPending(result.pending);
    state.pendingEdit = {client_id: item.client_id, text: result.pending.body || ""};
    redrawKeepingPlace();
    const box = $("#stream").querySelector(".pendedit");
    if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  } catch (error) { pendingConflict(error, item); }
}

async function savePendingEdit(item) {
  const edit = state.pendingEdit;
  if (!edit) return;
  const text = String(edit.text || "").trim();
  if (!text) { setReceipt("an empty message cannot be saved — cancel it instead", "warn"); return; }
  const current = state.pending.find((p) => p.client_id === item.client_id) || item;
  try {
    const result = await api(pendingPath(item.room, item.client_id), {
      method: "PATCH", body: {version: current.version, body: text, editing: false},
    });
    putPending(result.pending);
    state.pendingEdit = null;
    redrawKeepingPlace();
    schedulePendingPoll();
  } catch (error) { pendingConflict(error, item); }
}

async function stopPendingEdit(item) {
  const current = state.pending.find((p) => p.client_id === item.client_id) || item;
  try {
    const result = await api(pendingPath(item.room, item.client_id), {
      method: "PATCH", body: {version: current.version, editing: false},
    });
    putPending(result.pending);
  } catch (error) { pendingConflict(error, item); return; }
  state.pendingEdit = null;
  redrawKeepingPlace();
  schedulePendingPoll();
}

async function cancelPending(item) {
  const current = state.pending.find((p) => p.client_id === item.client_id) || item;
  try {
    const result = await api(pendingPath(item.room, item.client_id), {
      method: "DELETE", body: {version: current.version},
    });
    putPending(result.pending);
    state.pendingEdit = null;
    // The files this message was carrying are its own again.
    for (const entry of currentUploads(item.room)) {
      if (entry.attempt === item.client_id) { entry.attempt = null; entry.status = "ready"; }
    }
    saveUploadDrafts();
    renderUploadChips();
    redrawKeepingPlace();
    setReceipt("cancelled before the native session took it", "ok");
  } catch (error) { pendingConflict(error, item); }
}

/* Putting a settled message back is a new message the person chooses to
   write, never an automatic retry. */
function restorePending(item) {
  const draft = $("#draft");
  draft.value = draft.value.trim() ? draft.value.replace(/\s*$/, "\n") + item.body : item.body;
  state.pendingDismissed.add(item.client_id);
  scheduleDraftSave();
  redrawKeepingPlace();
  draft.focus();
}

/* Managed attachment thumbnails on messages UX46 itself delivered.

   The queue is the only correlation used: the native userMessage carries the
   client_id of the journal entry that delivered it, and that entry names the
   file ids it carried. Nothing is read from message text, turn position or
   local paths, so a native file UX46 did not send keeps its honest
   placeholder. The index is per room, fetched once and shared by every
   message in that room, and re-read on a short TTL so a file accepted a
   moment ago appears without a reload. It lives in memory only, so any device
   that can open the room sees the same thing. */
const ATTACHMENT_TTL_MS = 20000;
const ATTACHMENT_FILE_ID = /^[A-Za-z0-9_-]{1,128}$/;
const attachmentIndex = new Map();   // room -> {at, loading, byClient, signature}

function attachmentUrl(id, kind) {
  return apiUrl("/api/atlas/files/" + encodeURIComponent(id) + "/" + kind);
}

function managedAttachmentsFor(item) {
  const room = state.room;
  if (!room || !item.client_id) return [];
  const entry = attachmentIndex.get(room);
  if (!entry || Date.now() - entry.at > ATTACHMENT_TTL_MS) loadAttachmentIndex(room);
  return (entry && entry.byClient.get(String(item.client_id))) || [];
}

async function loadAttachmentIndex(room) {
  const existing = attachmentIndex.get(room);
  if (existing && existing.loading) return;          // one in-flight request per room
  const seq = state.roomSeq;
  const entry = existing || {at: 0, loading: false, byClient: new Map(), signature: ""};
  entry.loading = true;
  entry.at = Date.now();                             // never re-ask on every redraw
  attachmentIndex.set(room, entry);
  let data = null;
  try { data = await api("/api/room/" + encodeURI(room) + "/pending"); }
  catch (error) { entry.loading = false; return; }   // no thumbnail is better than a wrong one
  entry.loading = false;
  entry.at = Date.now();
  const byClient = new Map();
  const parts = [];
  for (const queued of (data.pending || [])) {
    const ids = (queued.attachments || [])
      .map((a) => String((a && a.file_id) || ""))
      .filter((id) => ATTACHMENT_FILE_ID.test(id));
    if (!ids.length || !queued.client_id) continue;
    byClient.set(String(queued.client_id), ids);
    parts.push(queued.client_id + ":" + ids.join(","));
  }
  const signature = parts.sort().join("|");
  const changed = signature !== entry.signature;
  entry.byClient = byClient;
  entry.signature = signature;
  if (changed && !stale(seq, room)) redrawWithAttachments();
}

function redrawWithAttachments() {
  const stream = $("#stream");
  if (!stream) return;
  const following = state.following && !state.sel && !state.anchor;
  redrawKeepingPlace();
  if (following) toTail();
}

/* A standalone placeholder line is hidden only where the queue confirms a file
   for that exact message, and only as many lines as there are files. Prose that
   merely mentions the token, and any extra placeholder, is left alone. */
const ATTACHMENT_PLACEHOLDER = /^\[(localImage|localFile)\]$/;
function withoutMatchedPlaceholders(text, limit) {
  let removed = 0;
  return String(text).split("\n").filter((line) => {
    if (removed < limit && ATTACHMENT_PLACEHOLDER.test(line.trim())) { removed += 1; return false; }
    return true;
  }).join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

function attachmentStrip(ids) {
  const strip = el("div", {class: "msg-files",
    "aria-label": ids.length === 1 ? "1 attachment" : ids.length + " attachments"});
  for (const id of ids) strip.appendChild(attachmentThumb(id));
  return strip;
}

/* The queue does not carry file names, so the accessible name says what is
   known and no more. The download endpoint supplies the original name. */
function attachmentThumb(id) {
  const slot = el("span", {class: "msg-file"});
  const label = "Image attachment";
  const button = el("button", {class: "msg-file-thumb", type: "button",
    title: label + " — open full size", "aria-label": label + " — open full size",
    on: {click: (event) => openAttachment(id, event.currentTarget)}});
  const image = el("img", {src: attachmentUrl(id, "preview"), alt: label});
  // Preview answers for raster images only; anything else becomes a plain download.
  image.addEventListener("error", () => slot.replaceChildren(attachmentFileLink(id)));
  button.appendChild(image);
  slot.appendChild(button);
  return slot;
}

function attachmentFileLink(id) {
  return el("a", {class: "msg-file-link", href: attachmentUrl(id, "download"), download: "",
    title: "Download attachment", "aria-label": "Attachment — download"},
    [replyIcon("download"), el("span", {text: "Attachment"})]);
}

let attachmentViewer = null;
let attachmentOpener = null;
function attachmentViewerNode() {
  if (attachmentViewer) return attachmentViewer;
  const dialog = el("dialog", {class: "file-dialog", "aria-label": "Attachment"});
  const image = el("img", {class: "file-full", alt: "Image attachment, full size"});
  const download = el("a", {class: "linkbtn", download: "", text: "Download"});
  const close = el("button", {class: "linkbtn", type: "button", text: "Close",
    on: {click: () => dialog.close()}});
  dialog.append(el("div", {class: "file-dialog-bar"}, [download, close]), image);
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {                 // Escape closes the same way
    image.removeAttribute("src");
    if (attachmentOpener && attachmentOpener.isConnected) attachmentOpener.focus();
    attachmentOpener = null;
  });
  document.body.appendChild(dialog);
  attachmentViewer = {dialog, image, download, close};
  return attachmentViewer;
}

function openAttachment(id, opener) {
  if (!ATTACHMENT_FILE_ID.test(String(id))) return;
  const viewer = attachmentViewerNode();
  viewer.image.src = attachmentUrl(id, "preview");
  viewer.download.href = attachmentUrl(id, "download");
  attachmentOpener = opener || null;
  viewer.dialog.showModal();
  viewer.close.focus();
}

setupListen();
boot();

window.__atlas = {state, selectRoom, switchAgent, agentId, apiUrl, agentKey,
                  agentTone, agentInitials, agentAvatar, renderAgentPicker,
                  toggleFocus, renderWorkToggle, readingAnchor,
                  renderRoomList, navState, navRecord, navGroupFor, navIsRecent,
                  navRecency, navSorted, navMoveTo, navSetAutomatic, navKey,
                  newConv, openNewConversation, closeNewConversation,
                  submitNewConversation, setNewConvAgent,
                  refreshTail, renderBoard, loadEarlier,
                  applyDictation, stopDictation, renderStream, renderTabs,
                  applyShell, openPanel, closeDock, toggleLeft, setLeft, isWide,
                  renderSourcePanel, loadBoard, saveBoard, renderBoardPanel, boardContext, syncFloaters, showView,
                  showRecovery, checkAttempt, recoverAttempts, renderRecoveries,
                  readAllAttempts, readOutboxFor, dismissAttempt, restoreRecovery,
                  tabLabel, renameTab, moveTab, openTabMenu, closeTabMenu,
                  layoutScope, deviceOnlyLayout, setLayoutScope, layoutRecord,
                  applySharedLayout, refreshSharedLayout, publishThisLayout,
                  createLiveDesktop, joinLiveDesktop, selectedLiveDesktopId,
                  liveDesktops, flushLayoutOps, sharedSnapshotTabs, shared,
                  uiSessionRef, openYourUx, designateYourUx,
                  loadDesktopState, saveDesktopState, saveDesktop, loadDesktop,
                  deleteDesktop, renameDesktop, openDesktopDialog, retryDesktopChange,
                  desktopSnapshotTabs, defaultDesktopName,
                  openSession, openTab, closeTab, renderTabs, readTabs, setBrowseAgent,
                  browseAgent, continueHere, attachingHere, refreshRoomState,
                  loadAttention, renderAttentionPanel, setAttentionOrder, attentionSorted,
                  openAttentionRow, agentPath, renderCrumb,
                  attentionView, attentionRowsFor, attentionTabs, toggleAttentionFold,
                  isAttentionDismissed, dismissAttentionNotice, restoreAttentionNotice,
                  currentUploads, renderUploadChips, uploadChosenFiles, uploadOne,
                  loadPending, pendingShown, setTheme, applyTheme, resizeDraft};
