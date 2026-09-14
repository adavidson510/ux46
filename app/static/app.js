const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  data: null,
  csrf: "",
  windowId: new URLSearchParams(location.search).get("window") || "window-main",
  focusedViewId: null,
  openAttentionId: null,
  terminals: new Map(),
  polls: new Map(),
  quiet: false,
};

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function button(className, text, handler) {
  const node = el("button", className, text);
  node.type = "button";
  if (handler) node.addEventListener("click", handler);
  return node;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") headers["X-Atlas-CSRF"] = state.csrf;
  const response = await fetch(path, { ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch { /* handled below */ }
  if (!response.ok) throw new Error(payload.error || `Atlas request failed (${response.status})`);
  return payload;
}

function notify(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { node.className = "toast"; }, 2800);
}

function projectColor(id) {
  const colors = { orbit: "#a75cff", "demo-pet": "#72d26b", nightwatch: "#4f94ff", general: "#93a2b6" };
  return colors[id] || `hsl(${[...String(id)].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360} 62% 63%)`;
}

function sessionById(identity) {
  return state.data.sessions.find((item) => item.identity === identity);
}

function activeWindow() {
  return state.data.workspace.windows.find((item) => item.id === state.windowId);
}

function humanState(session) {
  if (!session) return "Unavailable";
  const attention = { needs_now: "Needs you now", needs_soon: "Needs you soon", review_ready: "Review ready" };
  if (attention[session.attention_state]) return attention[session.attention_state];
  return ({ working: "Working", responding: "Responding", waiting_user: "Waiting", idle_open: "Idle open", stopped: "Stopped", archived: "Archived" })[session.execution_state] || "Saved";
}

function relativeDate(value) {
  if (!value) return "saved";
  const parsed = new Date(value.length === 10 ? `${value}T00:00:00` : value);
  const delta = Date.now() - parsed.getTime();
  if (!Number.isFinite(delta)) return value;
  const minutes = Math.max(0, Math.floor(delta / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

async function refresh() {
  const payload = await api("/api/bootstrap");
  state.data = payload;
  state.csrf = payload.csrf;
  const window = activeWindow();
  if (!window && state.windowId !== "window-main") {
    document.body.classList.add("detached");
    $("#workspaceCanvas").append(el("div", "workspace-empty", "This view has been attached or closed. You can close this window."));
    return;
  }
  state.focusedViewId = window?.focused_view_id || window?.views?.[0]?.id || null;
  renderAll();
}

function renderAll() {
  const detached = state.windowId !== "window-main";
  document.body.classList.toggle("detached", detached);
  $("#workspaceName").textContent = state.data.workspace.name;
  $("#workspaceRestore").textContent = `Restored ${relativeDate(state.data.workspace.last_opened_at)}`;
  renderProjects();
  renderWorkspace();
  renderAttention();
  renderVoice();
  syncAttentionShell(state.data.workspace.attention_drawer !== "collapsed" && !detached, false);
}

function renderProjects() {
  const list = $("#projectList");
  list.replaceChildren();
  for (const project of state.data.projects.slice(0, 8)) {
    const item = button("", "", () => showProjectSessions(project));
    item.style.setProperty("--project-accent", projectColor(project.id));
    item.append(el("span", "project-dot"), el("b", "", project.name));
    item.title = `${project.name}: ${project.session_count} saved sessions`;
    list.append(item);
  }
}

function currentViews() {
  return activeWindow()?.views || [];
}

function renderWorkspace() {
  const canvas = $("#workspaceCanvas");
  const layout = state.data.workspace.layout || "adaptive-four";
  canvas.className = `workspace-canvas layout-${layout}`;
  canvas.replaceChildren();
  const views = currentViews();
  if (!views.length) {
    const empty = el("div", "workspace-empty");
    const body = el("div");
    body.append(el("h2", "", "Your workspace is clear"));
    body.append(el("p", "", "Open a saved session without restarting or duplicating it."));
    body.append(button("", "Choose a session", openSessionPicker));
    empty.append(body);
    canvas.append(empty);
    return;
  }
  for (const view of views) canvas.append(sessionView(view));
  if (views.length < 4 && state.windowId === "window-main" && layout !== "focus") {
    const empty = el("div", "empty-slot");
    const body = el("div");
    body.append(el("p", "", "Add a session view"));
    body.append(button("", "+ Choose session", openSessionPicker));
    empty.append(body);
    canvas.append(empty);
  }
}

function sessionView(view) {
  const session = sessionById(view.session_id);
  const card = el("article", `session-view${view.id === state.focusedViewId ? " focused-view" : ""}`);
  card.dataset.project = session?.project || "general";
  card.dataset.attention = session?.attention_state || "none";
  card.dataset.view = view.id;
  card.addEventListener("mousedown", () => focusView(view.id));

  const header = el("header", "session-header");
  header.append(el("span", "session-sigil"));
  const title = el("div", "session-title");
  title.append(el("strong", "", session?.project_name || "Unknown"), document.createTextNode(" / "), el("span", "", session?.title || view.session_id));
  header.append(title);
  const tags = el("div", "view-tags");
  tags.append(el("span", "tag", session?.runtime ? `${session.runtime} session` : "context"));
  tags.append(el("span", "tag", view.representation.replace("_", " ")));
  header.append(tags);
  const presence = el("span", `session-presence attention-${session?.attention_state || "none"}`);
  presence.append(el("i"), document.createTextNode(humanState(session)));
  header.append(presence);
  header.append(el("span", "continuation-badge", session?.continuation?.mode === "exact" ? "Exact Resume" : "Portable"));
  header.append(viewMenu(view, session));
  card.append(header);

  const terminal = state.terminals.get(view.id);
  if (terminal) {
    card.append(terminalBody(view, terminal));
    return card;
  }

  const body = el("div", "session-body");
  body.append(el("div", "session-kicker", "Session summary"));
  const endpoint = session?.runtime_endpoint || {};
  body.append(el("div", "session-byline", `${endpoint.agent_name || session?.runtime || "Atlas"} · ${relativeDate(session?.meaningful_activity_at || session?.updated)}`));
  body.append(el("p", "session-summary", session?.summary || "This session has a portable re-entry record. Open its memory for the distilled context."));
  const facts = el("div", "session-facts");
  facts.append(fact("Continuation", session?.continuation?.mode === "exact" ? "Native runtime pointer verified locally" : "Portable project-owned context"));
  facts.append(fact("Location", session?.identity || view.session_id));
  body.append(facts);
  const actions = el("div", "session-actions");
  if (session?.exact_resume) actions.append(button("primary", "Open exact session", () => launchTerminal(view, session)));
  actions.append(button("", "Read memory", () => showSessionDetail(session?.identity)));
  actions.append(button("", "Listen", () => setAudioTarget(session?.identity)));
  body.append(actions);
  card.append(body);

  if (view.control_state === "controlled") card.append(sessionInput(view, session));
  else {
    const watched = el("div", "session-input watched");
    watched.append(el("span", "", "Watched here · input is controlled elsewhere"));
    watched.append(button("", "Take control", () => transferControl(view.id)));
    card.append(watched);
  }
  const footer = el("footer", "session-footer");
  footer.append(el("span", "", `Last activity ${relativeDate(session?.meaningful_activity_at || session?.updated)}`));
  footer.append(el("span", "spacer", view.control_state === "controlled" ? "● Controlled" : "◉ Watched"));
  footer.append(el("span", "", endpoint.transport || endpoint.machine_id || "portable"));
  card.append(footer);
  return card;
}

function fact(label, value) {
  const node = el("div", "fact");
  node.append(el("small", "", label), el("span", "", value || "Unknown"));
  return node;
}

function viewMenu(view, session) {
  const shell = el("div", "view-menu");
  const toggle = button("", "⋮", () => { menu.hidden = !menu.hidden; });
  toggle.setAttribute("aria-label", `View options for ${session?.title || view.session_id}`);
  const menu = el("menu");
  menu.hidden = true;
  if (state.windowId === "window-main") menu.append(menuItem("Detach to new window", () => detachView(view.id)));
  else menu.append(menuItem("Attach to main window", () => attachView(view.id)));
  menu.append(menuItem("Change representation", () => toggleRepresentation(view)));
  menu.append(menuItem("Close view", () => closeView(view.id)));
  shell.append(toggle, menu);
  return shell;
}

function menuItem(text, handler) {
  const item = document.createElement("li");
  item.append(button("", text, handler));
  return item;
}

function sessionInput(view, session) {
  const shell = el("div", "session-input");
  const label = el("label", "", `Session input (${session?.title || view.session_id})`);
  const form = el("form");
  const input = el("input");
  input.placeholder = `Message ${session?.title || "this session"}…`;
  input.autocomplete = "off";
  const voice = button("", "◉", () => setAudioTarget(session?.identity));
  voice.title = "Make this the voice target";
  const send = el("button", "", "➤");
  send.type = "submit";
  form.append(input, voice, send);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    if (!session?.exact_resume) return notify("This session has portable context but no exact live runtime.", true);
    try {
      const terminal = await ensureTerminal(view, session);
      await api(`/api/terminals/${terminal.panel_id}/input`, { method: "POST", body: JSON.stringify({ text, enter: true }) });
      input.value = "";
      renderWorkspace();
      setTimeout(() => pollTerminal(view.id), 100);
    } catch (error) { notify(error.message, true); }
  });
  shell.append(label, form);
  return shell;
}

async function ensureTerminal(view, session) {
  if (state.terminals.has(view.id)) return state.terminals.get(view.id);
  const terminal = await api("/api/terminals", { method: "POST", body: JSON.stringify({ identity: session.identity, title: session.title }) });
  state.terminals.set(view.id, terminal);
  return terminal;
}

async function launchTerminal(view, session) {
  notify(`Resuming ${session.title}…`);
  try {
    await ensureTerminal(view, session);
    renderWorkspace();
    setTimeout(() => pollTerminal(view.id), 80);
  } catch (error) { notify(error.message, true); }
}

function terminalBody(view, terminal) {
  const body = el("div", "terminal-body");
  const output = el("pre", "terminal-output", terminal.content || "Resuming native session…");
  output.dataset.output = view.id;
  const composer = el("form", "terminal-composer");
  const input = el("input");
  input.placeholder = "Send to this exact session…";
  const interrupt = button("", "⌃C", async () => {
    try { await api(`/api/terminals/${terminal.panel_id}/interrupt`, { method: "POST", body: "{}" }); }
    catch (error) { notify(error.message, true); }
  });
  const send = el("button", "", "Send"); send.type = "submit";
  composer.append(input, interrupt, send);
  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!input.value.trim()) return;
    const text = input.value; input.value = "";
    try {
      await api(`/api/terminals/${terminal.panel_id}/input`, { method: "POST", body: JSON.stringify({ text, enter: true }) });
      setTimeout(() => pollTerminal(view.id), 80);
    } catch (error) { notify(error.message, true); }
  });
  body.append(output, composer);
  setTimeout(() => pollTerminal(view.id), 80);
  return body;
}

async function pollTerminal(viewId) {
  const terminal = state.terminals.get(viewId);
  if (!terminal) return;
  clearTimeout(state.polls.get(viewId));
  try {
    const snapshot = await api(`/api/terminals/${terminal.panel_id}`);
    state.terminals.set(viewId, snapshot);
    const output = $(`[data-output="${viewId}"]`);
    if (output) {
      const bottom = output.scrollHeight - output.scrollTop - output.clientHeight < 80;
      output.textContent = snapshot.content || (snapshot.alive ? "Native session is starting…" : "Runtime stopped. The saved session remains available to resume.");
      if (bottom) output.scrollTop = output.scrollHeight;
    }
    if (snapshot.alive) state.polls.set(viewId, setTimeout(() => pollTerminal(viewId), 900));
  } catch (error) { notify(error.message, true); }
}

async function workspaceAction(action, body = {}) {
  const payload = await api(`/api/workspace/${action}`, { method: "POST", body: JSON.stringify(body) });
  state.data.workspace = payload.workspace;
  if (payload.attention_counts) state.data.attention_counts = payload.attention_counts;
  return payload;
}

async function focusView(viewId) {
  if (state.focusedViewId === viewId) return;
  state.focusedViewId = viewId;
  try { await workspaceAction("focus", { view_id: viewId }); } catch { /* visual focus still useful */ }
  const session = sessionById(currentViews().find((view) => view.id === viewId)?.session_id);
  $("#focusLabel").textContent = session?.title || "Workspace";
  renderWorkspace();
}

async function transferControl(viewId) {
  try { await workspaceAction("control", { view_id: viewId }); renderWorkspace(); notify("Session input control transferred."); }
  catch (error) { notify(error.message, true); }
}

async function closeView(viewId) {
  try {
    await workspaceAction("close", { view_id: viewId });
    state.terminals.delete(viewId);
    renderWorkspace();
    notify("View closed. The underlying session was not stopped.");
  } catch (error) { notify(error.message, true); }
}

async function detachView(viewId) {
  try {
    const payload = await workspaceAction("detach", { view_id: viewId });
    renderWorkspace();
    const child = window.open(`/?window=${encodeURIComponent(payload.window.id)}`, payload.window.id, "popup,width=640,height=720");
    if (!child) notify("The view was detached. Allow pop-ups to open its window.", true);
  } catch (error) { notify(error.message, true); }
}

async function attachView(viewId) {
  try {
    await workspaceAction("attach", { view_id: viewId });
    notify("View attached to the main Atlas workspace.");
    setTimeout(() => window.close(), 250);
  } catch (error) { notify(error.message, true); }
}

async function toggleRepresentation(view) {
  notify(`This is the ${view.representation.replace("_", " ")} representation. Open exact session switches it to the live TUI.`);
}

async function setAudioTarget(sessionId) {
  if (!sessionId) return;
  try {
    await workspaceAction("audio", { session_id: sessionId });
    renderVoice();
  } catch (error) { notify(error.message, true); }
}

function renderVoice() {
  const identity = state.data.workspace.active_audio_target;
  const session = sessionById(identity);
  $("#voiceTarget").textContent = session?.title || "No voice target";
  $("#voiceStatus").textContent = session ? "Ready to listen" : "Choose Listen on a session";
  $("#voiceDock").classList.toggle("listening", Boolean(session && $("#voiceToggle").getAttribute("aria-pressed") === "true"));
}

function openSessionPicker() {
  renderSessionOptions("");
  $("#sessionPicker").showModal();
  setTimeout(() => $("#sessionFilter").focus(), 30);
}

function renderSessionOptions(query) {
  const list = $("#sessionOptions");
  list.replaceChildren();
  const visible = state.data.sessions.filter((session) => `${session.identity} ${session.title} ${session.summary}`.toLowerCase().includes(query.toLowerCase()));
  for (const session of visible) {
    const item = button("session-option", "", () => openSession(session.identity));
    const dot = el("span", "project-dot"); dot.style.setProperty("--project-accent", projectColor(session.project));
    const labels = el("span"); labels.append(el("b", "", session.title), el("small", "", `${session.project_name} · ${session.summary || "Portable context"}`));
    item.append(dot, labels, el("small", "", session.continuation.mode === "exact" ? "Exact" : "Portable"));
    list.append(item);
  }
  if (!visible.length) list.append(el("div", "command-message", "No matching saved sessions."));
}

async function openSession(identity) {
  try {
    const payload = await workspaceAction("open", { session_id: identity, window_id: state.windowId });
    $("#sessionPicker").close();
    state.focusedViewId = payload.view.id;
    renderWorkspace();
    notify(payload.existing ? "Focused the existing session view." : "Session added to this workspace.");
  } catch (error) { notify(error.message, true); }
}

function showProjectSessions(project) {
  const results = state.data.sessions.filter((session) => session.project === project.id);
  showCommandResults({ kind: "session_list", sessions: results, project });
}

async function showSessionDetail(identity) {
  if (!identity) return;
  try {
    const session = await api(`/api/session?identity=${encodeURIComponent(identity)}`);
    $("#detailKicker").textContent = `${session.project_name} · project-owned memory`;
    $("#detailTitle").textContent = session.title;
    const body = $("#detailBody"); body.replaceChildren();
    const actions = el("div", "detail-actions");
    actions.append(button("", "Place in workspace", () => { $("#detailDialog").close(); openSession(identity); }));
    if (session.exact_resume) actions.append(button("", "Exact resume available", () => { $("#detailDialog").close(); openSession(identity); }));
    body.append(actions, el("pre", "", session.record));
    $("#detailDialog").showModal();
  } catch (error) { notify(error.message, true); }
}

async function runCommand(text) {
  const input = text.trim();
  if (!input) return;
  const results = $("#commandResults");
  results.hidden = false; results.replaceChildren(el("div", "command-message", "Atlas is finding the right context…"));
  try {
    const payload = await api("/api/command", { method: "POST", body: JSON.stringify({ text: input }) });
    if (["session_opened", "workspace_undo", "workspace_composed"].includes(payload.kind)) await refresh();
    showCommandResults(payload);
  } catch (error) {
    results.replaceChildren(el("div", "command-message", error.message));
  }
}

function showCommandResults(payload) {
  const results = $("#commandResults");
  results.hidden = false; results.replaceChildren();
  if (payload.kind === "session_list") {
    results.append(el("div", "command-message", `${payload.sessions.length} ${payload.project?.name || "Atlas"} session${payload.sessions.length === 1 ? "" : "s"}`));
    for (const session of payload.sessions) results.append(commandSession(session));
  } else if (payload.kind === "session_opened") {
    results.append(el("div", "command-message", `${payload.existing ? "Focused" : "Opened"} ${payload.session.title}.`));
    results.append(commandSession(payload.session));
  } else if (payload.kind === "workspace_undo") {
    results.append(el("div", "command-message", `Undid: ${payload.undone}`));
  } else if (payload.kind === "workspace_composed") {
    const unresolved = payload.unresolved || [];
    const message = `Composed ${payload.sessions.length} current session${payload.sessions.length === 1 ? "" : "s"}${unresolved.length ? `; ${unresolved.map((item) => item.name).join(", ")} has no saved session yet` : ""}.`;
    results.append(el("div", "command-message", message));
    for (const session of payload.sessions) results.append(commandSession(session));
  } else if (payload.kind === "search") {
    results.append(el("div", "command-message", `${payload.results.length} result${payload.results.length === 1 ? "" : "s"} across project context`));
    for (const result of payload.results) {
      const identity = result.identity || result.session_identity || result.project_session;
      const row = button("command-result", "", () => identity ? showSessionDetail(identity) : notify(result.path || "Result has no openable session"));
      row.append(el("b", "", result.title || result.name || identity || "Atlas result"));
      row.append(el("small", "", result.snippet || result.summary || result.path || ""));
      row.append(el("em", "", "Open"));
      results.append(row);
    }
  }
  if (!results.children.length) results.append(el("div", "command-message", "No matching context was found."));
}

function commandSession(session) {
  const row = button("command-result", "", () => openSession(session.identity));
  row.append(el("b", "", session.title), el("small", "", `${session.project_name} · ${session.summary || session.identity}`), el("em", "", "Open"));
  return row;
}

function activeAttention() {
  return state.data.attention.filter((item) => ["new", "seen", "opened", "answered", "resuming"].includes(item.lifecycle_state));
}

function renderAttention() {
  const items = activeAttention();
  const counts = state.data.attention_counts || { needs_now: 0, needs_soon: 0, review_ready: 0 };
  const total = counts.needs_now + counts.needs_soon + counts.review_ready;
  for (const id of ["#attentionTotal", "#drawerAttentionCount", "#navAttentionCount"]) {
    $(id).textContent = total; $(id).hidden = total === 0;
  }
  $("#attentionSummary").textContent = total ? `${counts.needs_now} now · ${counts.needs_soon} waiting · ${counts.review_ready} review` : "Nothing needs you";
  const list = $("#attentionList"); list.replaceChildren();
  if (!items.length) {
    const empty = el("div", "attention-empty");
    const body = el("div"); body.append(el("h3", "", "You’re clear"), el("p", "", "Only work that genuinely requires you appears here. Recoverable agent issues stay out."));
    empty.append(body); list.append(empty); return;
  }
  const classes = [
    ["needs_now", "Needs you now"], ["needs_soon", "Needs you soon"], ["review_ready", "Review when ready"],
  ];
  for (const [kind, label] of classes) {
    const groupItems = items.filter((item) => item.interruption_class === kind);
    if (!groupItems.length) continue;
    const group = el("section", "attention-group"); group.dataset.attention = kind;
    group.append(el("div", "attention-group-title", `${label} · ${groupItems.length}`));
    for (const item of groupItems) group.append(attentionItem(item));
    list.append(group);
  }
  const newest = items.find((item) => item.interruption_class === "needs_now" && item.lifecycle_state === "new");
  if (newest && !state.quiet) showAttentionToast(newest);
}

function attentionItem(item) {
  const shell = el("article", "attention-item");
  const title = el("h3", "", item.need_from_aaron);
  title.append(el("span", "lifecycle", item.lifecycle_state));
  shell.append(title);
  shell.append(el("p", "", `${projectName(item.project_id)} / ${sessionById(item.session_id)?.title || item.session_id}`));
  shell.append(el("p", "", `Requested by ${item.requested_by} · ${item.source?.runtime || "runtime"} · ${item.source?.machine || "unknown"} · ${relativeDate(item.created_at)}`));
  shell.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    openAttention(item);
  });
  if (state.openAttentionId === item.id) shell.append(attentionDetail(item));
  return shell;
}

function attentionDetail(item) {
  const detail = el("div", "attention-detail");
  const fields = [
    ["Need from User", item.need_from_aaron], ["Why User", item.why_aaron], ["Paused", item.paused],
    ["Still continuing", item.still_continuing], ["If no action", item.if_no_action], ["Recommendation", item.recommendation || "No recommendation"],
    ["After response", item.after_response],
  ];
  const dl = document.createElement("dl");
  for (const [name, value] of fields) dl.append(el("dt", "", name), el("dd", "", value));
  detail.append(dl);
  if (item.lifecycle_state === "answered") detail.append(el("p", "", "Answer sent. Waiting for the source runtime to acknowledge before Atlas marks work as resuming."));
  if (item.lifecycle_state === "resuming") detail.append(el("p", "", "The source acknowledged your answer and is resuming dependent work."));
  if (item.authoritative_action?.mode === "inline" && ["new", "seen", "opened"].includes(item.lifecycle_state)) {
    const choices = el("div", "attention-options");
    for (const option of item.options || []) choices.append(button("", `Choose ${option.id}: ${option.label}`, () => answerAttention(item, { answer_type: "option", value: option.id })));
    if (!(item.options || []).length) choices.append(button("", "Respond in session", () => openAttentionSession(item)));
    detail.append(choices);
  }
  const actions = el("div", "attention-actions");
  actions.append(button("open-session", "Open session", () => openAttentionSession(item)));
  if (item.authoritative_action?.mode === "external") actions.append(button("", `Open ${item.authoritative_action.system || "authoritative system"}`, () => openExternalAttention(item)));
  detail.append(actions);
  return detail;
}

async function openAttention(item) {
  state.openAttentionId = state.openAttentionId === item.id ? null : item.id;
  if (["new", "seen"].includes(item.lifecycle_state)) {
    try {
      const payload = await api("/api/attention/open", { method: "POST", body: JSON.stringify({ id: item.id }) });
      state.data.attention = payload.attention_items; state.data.attention_counts = payload.attention_counts;
    } catch (error) { notify(error.message, true); }
  }
  renderAttention();
}

async function answerAttention(item, answer) {
  try {
    const payload = await api("/api/attention/answer", { method: "POST", body: JSON.stringify({ id: item.id, answer }) });
    state.data.attention = payload.attention_items; state.data.attention_counts = payload.attention_counts;
    renderAttention(); notify("Answer recorded and sent back to the source runtime.");
  } catch (error) { notify(error.message, true); }
}

async function openAttentionSession(item) {
  syncAttentionShell(false);
  await openSession(item.session_id);
}

function openExternalAttention(item) {
  const ref = item.authoritative_action?.destination_ref;
  if (ref && /^https?:\/\//.test(ref)) window.open(ref, "_blank", "noopener");
  else notify(`Complete this in ${item.authoritative_action?.system || "the authoritative system"}; Atlas will wait for its completion signal.`);
}

function projectName(id) {
  return state.data.projects.find((project) => project.id === id)?.name || id;
}

function syncAttentionShell(open, persist = true) {
  $("#app").classList.toggle("attention-open", open);
  $("#attentionToggle").setAttribute("aria-expanded", String(open));
  $("#attentionRail").dataset.state = open ? "expanded" : "collapsed";
  if (persist && state.data) workspaceAction("attention-drawer", { value: open ? "expanded" : "collapsed" }).catch((error) => notify(error.message, true));
}

function showAttentionToast(item) {
  const toast = $("#attentionToast");
  $("#attentionToastTitle").textContent = `${projectName(item.project_id)} / ${sessionById(item.session_id)?.title || item.session_id} needs your decision`;
  $("#attentionToastBody").textContent = `${item.requested_by} · ${item.source?.runtime || "runtime"} · ${relativeDate(item.created_at)}`;
  toast.hidden = false;
  $("#attentionToastOpen").onclick = () => { toast.hidden = true; syncAttentionShell(true); openAttention(item); };
}

async function setLayout(layout) {
  try { await workspaceAction("layout", { layout }); renderWorkspace(); notify(`Layout changed to ${layout.replace("-", " ")}.`); }
  catch (error) { notify(error.message, true); }
}

function bindEvents() {
  $("#commandForm").addEventListener("submit", (event) => { event.preventDefault(); runCommand($("#commandInput").value); });
  $("#sessionFilter").addEventListener("input", (event) => renderSessionOptions(event.target.value));
  $("#detailClose").addEventListener("click", () => $("#detailDialog").close());
  $("#attentionToggle").addEventListener("click", () => syncAttentionShell(true));
  $("#attentionClose").addEventListener("click", () => syncAttentionShell(false));
  $("#attentionToastDismiss").addEventListener("click", () => { $("#attentionToast").hidden = true; });
  $("#allProjectsButton").addEventListener("click", openSessionPicker);
  $("#layoutButton").addEventListener("click", () => {
    const layouts = ["adaptive-four", "split", "focus", "stack"];
    const index = layouts.indexOf(state.data.workspace.layout || "adaptive-four");
    setLayout(layouts[(index + 1) % layouts.length]);
  });
  $("#focusButton").addEventListener("click", () => setLayout(state.data.workspace.layout === "focus" ? "adaptive-four" : "focus"));
  $("#undoButton").addEventListener("click", async () => {
    try { const payload = await workspaceAction("undo"); renderWorkspace(); notify(`Undid: ${payload.undone}`); }
    catch (error) { notify(error.message, true); }
  });
  $("#voiceToggle").addEventListener("click", (event) => {
    const active = event.currentTarget.getAttribute("aria-pressed") !== "true";
    event.currentTarget.setAttribute("aria-pressed", String(active));
    $("#voiceStatus").textContent = active ? "Listening…" : "Ready to listen";
    renderVoice();
  });
  $("#voiceStop").addEventListener("click", async () => {
    try { await workspaceAction("audio", { session_id: null }); $("#voiceToggle").setAttribute("aria-pressed", "false"); renderVoice(); }
    catch (error) { notify(error.message, true); }
  });
  $("#quietHours").addEventListener("click", () => {
    state.quiet = !state.quiet;
    $("#quietHours span").textContent = state.quiet ? "On" : "Turn on";
    if (state.quiet) $("#attentionToast").hidden = true;
  });
  $$("[data-route]").forEach((node) => node.addEventListener("click", () => {
    const route = node.dataset.route;
    if (route === "attention") syncAttentionShell(true);
    else if (route === "sessions" || route === "projects") openSessionPicker();
    else if (route === "memory") { $("#commandInput").focus(); $("#commandInput").value = "Find "; }
  }));
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#commandInput").focus(); }
    if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "a") { event.preventDefault(); syncAttentionShell(!$("#app").classList.contains("attention-open")); }
    if (event.key === "Escape") { $("#commandResults").hidden = true; if ($("#app").classList.contains("attention-open")) syncAttentionShell(false); }
  });
}

bindEvents();
refresh().catch((error) => {
  $("#workspaceCanvas").replaceChildren(el("div", "workspace-empty", `Atlas could not load: ${error.message}`));
});
