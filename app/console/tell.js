/* Tell boards are a quiet, read-first view over the local Tell service. The
   console remains alive underneath it: no room, draft, tab, or agent changes
   when a person reads a board. */
"use strict";

(() => {
  const BOARD_DEFAULT = "working-better";
  const POLL_MS = 15000;
  const cursorKey = "ux46.tell.activity.cursor";
  const tell = {
    summary: null,
    board: BOARD_DEFAULT,
    boardPayload: null,
    boardGeneration: 0,
    loading: false,
    poll: null,
    cursor: "",
    baseline: false,
    activityNotice: false,
    activityPulse: false,
    pulseTimer: null,
    observer: null,
    layoutObserver: null,
    replyDrafts: new Map(),
    pendingRender: false,
    contentSignature: "",
    focusDraft: null,
    focusRevision: null,
    settingsError: "",
  };

  function node(tag, attrs, children) {
    return el(tag, attrs || {}, children || []);
  }
  function viewOpen() { return !$("#viewTell").hidden; }
  function pageVisible() { return document.visibilityState === "visible"; }
  function stamp(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value || "");
    return date.toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
  }
  function boardLabel(id) {
    const labels = {"working-better":"Ideas to try", "bigger-picture":"Across projects", "invention-watch":"Possibilities", "daily-review":"Project roundup", "activity":"Activity"};
    if (labels[id]) return labels[id];
    const found = (tell.summary && tell.summary.boards || []).find((board) => board.id === id);
    return (found && found.label) || id.replace(/-/g, " ");
  }
  function authorLabel(value) {
    const names = {"local-workspace": "Local", "cp-workspace": "CP", "pane-workspace": "Pane",
      "agent3-workspace": "Agent3", "cairn-workspace": "Cairn", "cc-workspace": "CC", "agent2-orbit": "Agent2"};
    return names[value] || value;
  }
  function tellIcon(kind, tiled = false) {
    const paths = {
      review: "M5 2v3m10-3v3M3 7h14M4 3.5h12a1 1 0 0 1 1 1V17H3V4.5a1 1 0 0 1 1-1ZM6 10h2m4 0h2m-8 3h2m4 0h2",
      focus: "M15.5 4.5a7 7 0 1 0 1.5 7M13 7a4 4 0 1 0 1 3M10 10l7-7m-4 0h4v4",
      idea: "M7 14c0-2-3-3-3-6a6 6 0 0 1 12 0c0 3-3 4-3 6M7 14h6M7.5 17h5M8 8l2 2 2-2m-2 2v4",
      connection: "M7.5 12.5l5-5M7 6l1-1a4 4 0 0 1 6 6l-1 1M7 8l-1 1a4 4 0 0 0 6 6l1-1",
      activity: "M3 10h3l2-6 4 12 2-6h3",
      close: "M5 5l10 10M15 5L5 15",
      check: "M4 10l4 4 8-8",
      comment: "M3 4h14v10H8l-5 3V4Z",
      edit: "m12 4 4 4M4 12l9-9 4 4-9 9-5 1 1-5Z",
      settings: "M3 6h5m4 0h5M3 14h9m4 0h1M10 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4m4 8a2 2 0 1 0 0 4 2 2 0 0 0 0-4",
    };
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("class", "tell-icon");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", paths[kind] || paths.connection);
    svg.appendChild(path);
    return tiled ? node("span", {class: "tell-icon-tile tell-icon-" + kind, "aria-hidden": "true"}, [svg]) : svg;
  }
  function tellMark() {
    return node("span", {class: "tell-mark", "aria-hidden": "true"},
      ["dot", "bar", "tall", "dot"].map(part => node("i", {class: "tell-mark-" + part})));
  }
  function safeRef(value) {
    try {
      const url = new URL(String(value || ""));
      return /^https?:$/.test(url.protocol) ? url.href : "";
    } catch (_) { return ""; }
  }
  function saveCursor(value) {
    try { window.localStorage.setItem(cursorKey, String(value || "")); } catch (_) { /* private browsing */ }
  }
  function readCursor() {
    try { return window.localStorage.getItem(cursorKey) || ""; } catch (_) { return ""; }
  }
  function updateNav() {
    const unread = Number((tell.summary || {}).unread_count || 0);
    const badge = $("#tellCount");
    badge.textContent = String(unread);
    badge.hidden = unread === 0;
    const button = $("#btnTell");
    button.classList.toggle("has-activity", tell.activityNotice);
    button.classList.toggle("pulse", tell.activityPulse);
    button.setAttribute("aria-label", unread ? "Signals · " + unread + " unread reviews or findings" : "Signals");
    button.title = unread ? "Signals · " + unread + " unread" : "Signals";
  }
  function enabled() { return Boolean(tell.summary && tell.summary.enabled); }
  function announceActivity() {
    tell.activityNotice = true;
    tell.activityPulse = true;
    if (tell.pulseTimer) clearTimeout(tell.pulseTimer);
    tell.pulseTimer = setTimeout(() => {
      tell.activityPulse = false;
      updateNav();
    }, 1800);
  }
  function editingReply() {
    const active = document.activeElement;
    return Boolean(active && active.closest && active.closest(".tell-reply, .tell-focus-form"));
  }
  function boardTabs() {
    const nav = node("div", {class: "tell-tabs", role: "tablist", "aria-label": "Signals"});
    const order = ["working-better", "bigger-picture", "invention-watch", "daily-review", "activity"];
    for (const board of [...(tell.summary.boards || [])].sort((a,b)=>order.indexOf(a.id)-order.indexOf(b.id))) {
      nav.appendChild(node("button", {class: "tell-tab" + (board.id === tell.board ? " on" : ""),
        type: "button", role: "tab", "aria-selected": String(board.id === tell.board),
        id: "tell-tab-" + board.id, "aria-controls": "tell-board-panel",
        tabindex: board.id === tell.board ? "0" : "-1", text: boardLabel(board.id),
        on: {click: () => switchBoard(board.id), keydown: (event) => {
          const tabs = [...nav.querySelectorAll('[role="tab"]')];
          const current = tabs.indexOf(event.currentTarget);
          const next = event.key === "ArrowRight" ? (current + 1) % tabs.length
            : event.key === "ArrowLeft" ? (current + tabs.length - 1) % tabs.length
            : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
          if (next < 0) return;
          event.preventDefault(); tabs[next].focus(); tabs[next].click();
        }}}, [
        Number(board.unread_count || 0) ? node("span", {class: "tell-tab-count", text: String(board.unread_count)}) : null,
      ]));
    }
    return nav;
  }
  function scheduleCard(schedule) {
    const s = schedule || {};
    const hour = Number.isInteger(s.hour) && s.hour >= 0 && s.hour <= 23 ? s.hour : 2;
    const time = (hour % 12 || 12) + ":00 " + (hour < 12 ? "AM" : "PM");
    const timezone = s.timezone || "America/Los_Angeles";
    const row = node("section", {class: "tell-schedule", "aria-label": "Daily review schedule"}, [
      tellIcon("review", true),
      node("div", {class: "tell-schedule-copy"}, [
        node("strong", {text: s.enabled ? "Daily review is on" : "Daily review is paused"}),
        node("p", {text: s.enabled
          ? "Scheduled for " + time + " " + timezone + "."
          : "Resume when you want a daily review."}),
        s.next_run ? node("span", {class: "tell-muted", text: "Next: " + stamp(s.next_run)}) : null,
        s.error ? node("span", {class: "tell-error", text: s.error}) : null,
      ]),
      node("button", {class: "ghost tell-schedule-toggle", type: "button",
        "aria-pressed": String(Boolean(s.enabled)), text: s.enabled ? "Pause" : "Resume",
        on: {click: () => setSchedule(!s.enabled)}}, []),
    ]);
    return row;
  }
  function sourceNode(source) {
    const label = String(source.label || "Source");
    const ref = String(source.ref || "");
    const href = safeRef(ref);
    return href
      ? node("a", {class: "tell-source", href, target: "_blank", rel: "noopener noreferrer", text: label})
      : node("span", {class: "tell-source", text: ref ? label + " · " + ref : label});
  }
  function isReviewFailure(post) {
    return post.kind === "review_failure" || (post.board === "daily-review" &&
      post.author === "Tell scheduler" && / · Review needs attention$/.test(post.title || ""));
  }
  function reviewHistory(posts) {
    const history = node("details", {class: "tell-review-history", data: {tellDetail: "review-history"}}, [
      node("summary", {text: "Review history · " + posts.length + " unsuccessful " + (posts.length === 1 ? "attempt" : "attempts")}),
    ]);
    for (const post of posts) {
      const detail = node("details", {class: "tell-review-attempt", data: {tellDetail: "post-" + post.id}}, [
        node("summary", {text: stamp(post.created_at) + " · Review did not complete"}),
        node("p", {text: post.human_body || post.body || "No review was produced."}),
      ]);
      for (const reply of post.replies || []) detail.appendChild(node("p", {class: "tell-muted", text: authorLabel(reply.author) + ": " + reply.body}));
      if (post.unread) detail.appendChild(node("button", {class: "linkbtn", type: "button", text: "Mark read", on: {click: () => void markSeen(post, tell.board, tell.boardGeneration, detail)}}));
      history.appendChild(detail);
    }
    return history;
  }
  function postNode(post, boardId, generation) {
    const legacy = String(post.body || "");
    const footerAt = legacy.indexOf("\n\nReview coverage:");
    const human = post.human_body || (footerAt >= 0 ? legacy.slice(0, footerAt) : legacy);
    const technical = post.technical_body || (footerAt >= 0 ? legacy.slice(footerAt).trim() : "");
    const article = node("article", {class: "tell-post", data: {post: post.id}}, [
      node("div", {class: "tell-post-head"}, [
        tellIcon({"daily-review": "review", "working-better": "idea", "bigger-picture": "connection", "invention-watch": "idea"}[boardId], true),
        node("div", {class: "tell-post-heading"}, [
          node("span", {class: "tell-category", text: boardLabel(boardId)}),
          node("h3", {text: post.title || "Something worth noticing"}),
          node("p", {class: "tell-byline", text: [authorLabel(post.author), stamp(post.created_at)].filter(Boolean).join(" · ")})]),
        post.unread ? node("span", {class: "tell-new", text: "New"}) : null,
      ]), node("div", {class: "tell-post-body md"}),
    ]);
    article.querySelector(".tell-post-body").appendChild(markdownFragment(human));
    article.dataset.originalTitle = post.title || "Something worth noticing";
    if (post.unread) article.querySelector(".tell-post-head").appendChild(node("button", {
      class: "linkbtn tell-mark-read", type: "button", text: "Mark read",
      on: {click: () => void markSeen(post, boardId, generation, article)}}));
    const audit = node("details", {class: "tell-audit", data: {tellDetail: "post-" + post.id}}, [
      node("summary", {text: "Full note, sources & discussion" + ((post.replies || []).length ? " · " + post.replies.length : "")}),
    ]);
    const original = node("div", {class:"md tell-original"});
    original.appendChild(markdownFragment(human)); audit.appendChild(original);
    if (technical || (post.human_body && legacy !== human)) {
      const prose = node("div", {class: "md tell-technical"});
      prose.appendChild(markdownFragment(technical || legacy)); audit.appendChild(prose);
    }
    if ((post.sources || []).length) audit.appendChild(node("div", {class: "tell-sources"}, post.sources.map(sourceNode)));
    const replies = post.replies || [];
    const replyList = node("div", {class: "tell-replies"});
    for (const reply of replies) replyList.appendChild(node("p", {text: [authorLabel(reply.author), reply.body].filter(Boolean).join(": ")}));
    if (!replies.length) replyList.appendChild(node("p", {text: "No discussion yet. Comments don't wake another agent."}));
    audit.appendChild(replyList); article.appendChild(audit);
    const feedback = node("div", {class: "tell-feedback"});
    if (window.__work) void window.__work.signal(post, article);
    const comment = node("details", {class: "tell-comment", data: {tellDetail: "comment-" + post.id}}, [
      node("summary", {}, [tellIcon("comment"), node("span", {text: "Add a thought"})]),
    ]);
    const form = node("form", {class: "tell-reply"}, [
      node("label", {class: "sr-only", for: "tell-reply-" + post.id, text: "Add a comment"}),
      node("textarea", {id: "tell-reply-" + post.id, rows: "2", maxlength: "4000", placeholder: "What would make this more useful?",
        text: tell.replyDrafts.get(post.id) || ""}),
      node("button", {class: "ghost", type: "submit", text: "Comment"}),
    ]);
    form.addEventListener("submit", (event) => { event.preventDefault(); void addReply(post.id, form); });
    form.querySelector("textarea").addEventListener("input", (event) => tell.replyDrafts.set(post.id, event.currentTarget.value));
    form.querySelector("textarea").addEventListener("blur", () => {
      if (tell.pendingRender) { tell.pendingRender = false; render(false); }
    });
    comment.appendChild(form); article.appendChild(node("div", {class: "tell-post-actions"}, [feedback, comment]));
    return article;
  }
  function discoveryCard() {
    const d = tell.summary.discovery;
    if (!d) return null;
    const card = node("details", {class: "tell-discovery", data: {tellDetail:"configuration"}}, [
      node("summary", {text:"What Signals looks for & settings"}),
      node("div", {class: "tell-focus-heading"}, [tellIcon("focus"),
        node("p", {class: "tell-kicker", text: "WHAT WE'RE EXPLORING"})]),
      node("p", {class: "tell-focus-copy", text: d.focus || "Find useful connections across your work and what matters to you."}),
    ]);
    const editor = node("details", {data: {tellDetail: "focus"}}, [node("summary", {}, [tellIcon("edit"), node("span", {text: "Change direction"})])]);
    const form = node("form", {class: "tell-focus-form"});
    const field = node("textarea", {rows: "3", maxlength: "1600", "aria-label": "Signals discovery focus",
      text: tell.focusDraft === null ? d.focus || "" : tell.focusDraft});
    field.addEventListener("input", () => {
      if (tell.focusRevision === null) tell.focusRevision = d.revision;
      tell.focusDraft = field.value;
    });
    form.append(field, node("p", {class: "tell-muted", text: "Start broad. Change it as we learn. This guides ideas; it doesn't authorize actions."}),
      node("button", {type: "submit", class: "ghost", text: "Save direction"}));
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); const button = form.querySelector("button"); button.disabled = true;
      await saveDiscovery({discovery_focus: field.value, revision: tell.focusRevision ?? d.revision}, true);
      if (button.isConnected) button.disabled = false;
    });
    editor.appendChild(form);
    if (tell.settingsError) card.appendChild(node("p", {class: "tell-error", role: "status", text: tell.settingsError}));
    const controls = node("details", {class: "tell-audit", data: {tellDetail: "budget"}}, [
      node("summary", {}, [tellIcon("settings"), node("span", {text: "Participation & usage"})]),
      node("p", {text: "Agents may take a short look while already working. No extra agent turns or automatic reply chains."}),
    ]);
    const toggle = node("button", {class: "ghost", type: "button", text: d.participation_enabled ? "Pause check-ins" : "Allow check-ins"});
    toggle.addEventListener("click", () => saveDiscovery({participation_enabled: !d.participation_enabled, revision: d.revision}));
    const cap = node("select", {"aria-label": "Daily check-in limit"}, [0,2,4,6,8,12].map(n => {
      const option = node("option", {value: String(n), text: n + " check-ins per day"});
      option.selected = n === d.max_participations_per_day; return option;
    }));
    cap.addEventListener("change", () => saveDiscovery({max_participations_per_day: Number(cap.value), revision: d.revision}));
    controls.appendChild(node("div", {class: "tell-budget-controls"}, [toggle, cap]));
    const u = d.usage || {};
    const review = u.review || u;
    const count = Number.isFinite(u.participations?.count) ? u.participations.count : null;
    const number = value => Number.isFinite(value) ? value.toLocaleString() : "not measured";
    controls.appendChild(node("p", {class: "tell-muted", text: "Today's check-ins: " + number(count) + " / " + d.max_participations_per_day
      + ". Review tokens: " + number(review.input_tokens) + " in · " + number(review.output_tokens) + " out."}));
    controls.appendChild(node("p", {class: "tell-muted", text: "Reading this screen uses no model calls. Extra cost inside an existing conversation isn't separately measured; these limits don't claim a hard token ceiling or total fleet usage."}));
    const audit = d.audit || u.audit || [];
    for (const entry of audit.slice(0,20)) controls.appendChild(node("p", {class: "tell-audit-entry", text:
      [entry.agent, entry.session, stamp(entry.when || entry.at || entry.created_at), entry.outcome || entry.status].filter(Boolean).join(" · ")}));
    card.appendChild(node("div", {class: "tell-discovery-controls"}, [editor, controls])); return card;
  }
  async function saveDiscovery(changes, clearDraft = false) {
    tell.settingsError = "";
    try {
      const result = await api("/api/tell/settings", {method: "PATCH", body: changes, absolute: true});
      if (result.discovery) tell.summary.discovery = result.discovery;
      if (clearDraft) { tell.focusDraft = null; tell.focusRevision = null; }
      document.activeElement?.blur();
      await refresh(); render(true);
    } catch (error) {
      tell.settingsError = "Not saved: " + error.message + ". Your text is kept here.";
      document.activeElement?.blur(); render(true);
    }
  }
  function activityNode(payload) {
    const activity = payload || {};
    const coverage = activity.coverage || {};
    const scope = String(coverage.scope || "local_mac").replace(/[_-]/g, " ");
    const place = /local\s*mac/i.test(scope) ? "Local Mac" : scope || "Local Mac";
    const unavailable = !activity || coverage.status === "unavailable" || coverage.status === "error";
    const knownToday = Number.isFinite(activity.total_today);
    const knownWeek = Number.isFinite(activity.total_7d);
    const countText = unavailable ? "Activity unavailable on " + place + "."
      : (knownToday ? activity.total_today : "Unknown") + " today · "
        + (knownWeek ? activity.total_7d : "Unknown") + " in the last 7 days";
    const sec = node("section", {class: "tell-activity"}, [
      node("div", {class: "tell-section-head"}, [
        node("div", {}, [node("h2", {text: "Activity"}),
          node("p", {text: countText}),
          node("span", {class: unavailable ? "tell-error" : "tell-muted", text: unavailable
            ? (coverage.error || "Tell activity is not available on this Mac.")
            : "Coverage: " + place + (coverage.days ? " · " + coverage.days + " days" : "")})]),
      ]),
    ]);
    const events = activity.events || [];
    if (!events.length && !unavailable) sec.appendChild(node("p", {class: "tell-empty", text: "No recent Tell traffic."}));
    for (const event of events) {
      sec.appendChild(node("div", {class: "tell-event"}, [
        node("span", {class: "tell-event-intent", text: event.intent || "message"}),
        node("div", {}, [node("p", {text: event.summary || "Tell activity"}),
          node("span", {class: "tell-muted", text: [authorLabel(event.sender), authorLabel(event.receiver), stamp(event.at)].filter(Boolean).join(" · ")})]),
      ]));
    }
    return sec;
  }
  function render(force) {
    const host = $("#tellBody");
    if (!tell.summary) {
      host.replaceChildren(node("p", {class: "tell-empty", text: "Connecting to Signals…"}));
      return;
    }
    if (!enabled()) {
      host.replaceChildren(node("section", {class: "tell-unavailable"}, [
        node("h2", {text: "Signals are unavailable"}),
        node("p", {text: "This console can continue normally while Tell is not enabled here."}),
      ]));
      return;
    }
    if (editingReply()) { tell.pendingRender = true; return; }
    if (tell.observer) { tell.observer.disconnect(); tell.observer = null; }
    if (tell.layoutObserver) { tell.layoutObserver.disconnect(); tell.layoutObserver = null; }
    const payload = tell.boardPayload;
    const signature = JSON.stringify([tell.board, tell.summary.schedule, tell.summary.discovery, payload]);
    if (!force && signature === tell.contentSignature) return;
    tell.contentSignature = signature;
    const focusedTab = document.activeElement?.closest(".tell-tab");
    const openDetails = new Set([...host.querySelectorAll("details[data-tell-detail][open]")].map(n => n.dataset.tellDetail));
    queueMicrotask(() => {
      for (const detail of host.querySelectorAll("details[data-tell-detail]")) detail.open = openDetails.has(detail.dataset.tellDetail);
      if (focusedTab) host.querySelector('.tell-tab[aria-selected="true"]')?.focus({preventScroll: true});
    });
    host.replaceChildren();
    host.appendChild(node("header", {class: "tell-head"}, [
      tellMark(),
      node("div", {class: "tell-heading"}, [node("h1", {text: "Signals"}),node("span", {class:"signals-attribution",text:"Connected by Tell"}),
        node("p", {class: "tell-lede", text: "A few ideas from your work. Keep the ones worth exploring."})]),
      node("span", {class: "tell-header-art", "aria-hidden": "true"}, [node("i"), node("i"), node("i")]),
      node("button", {class: "tell-close", type: "button", "aria-label": "Close Signals and return to conversation",
        title: "Return to conversation", on: {click: () => $("#btnConsole").click()}}, [tellIcon("close")]),
    ]));
    const discovery = discoveryCard();
    host.appendChild(node("div", {class:"tell-workflow"}, [
      node("p", {text:"Read the idea → choose a room → let the agent assess it."}),
      node("p", {class:"tell-muted",text:"Dismiss what is not useful, or remind yourself tomorrow. Adding an idea to a room does not start work. The room proposes a small test; afterward, record whether it helped."})
    ]));
    host.appendChild(boardTabs());
    if (tell.board === "daily-review") { const schedule = node("details", {class:"tell-review-history",data:{tellDetail:"schedule"}}, [node("summary",{text:"Roundup schedule"}),scheduleCard(tell.summary.schedule)]); host.appendChild(schedule); }
    if (!payload) { host.appendChild(node("p", {class: "tell-empty", text: "Loading " + boardLabel(tell.board) + "…"})); return; }
    if (tell.board === "activity") {
      const activity = activityNode(payload.activity || payload);
      activity.id = "tell-board-panel";
      activity.setAttribute("role", "tabpanel");
      activity.setAttribute("aria-labelledby", "tell-tab-" + tell.board);
      host.appendChild(activity);
      return;
    }
    const explanations = {"daily-review": "Recent project notes. These summaries are background, not a to-do list.", "working-better": "Small changes that could save you time or effort.", "bigger-picture": "How your projects might help each other.", "invention-watch": "Ideas that might be worth exploring—not claims of novelty or patentability."};
    const failures = (payload.posts || []).filter(isReviewFailure);
    const posts = (payload.posts || []).filter(post => !isReviewFailure(post));
    const section = node("section", {class: "tell-posts", id: "tell-board-panel", role: "tabpanel",
      "aria-labelledby": "tell-tab-" + tell.board}, [
      node("div", {class: "tell-section-head"}, [node("div", {}, [
        node("h2", {text: tell.board === "daily-review" ? "Latest roundup" : boardLabel(tell.board)}),
        node("p", {text: explanations[tell.board] || ""})]),
        node("span", {class: "tell-item-count", text: posts.length ? posts.length + (posts.length === 1 ? " note" : " notes") : "No findings yet"})]),
    ]);
    if (!posts.length) section.appendChild(node("p", {class: "tell-empty", text: failures.length ? "No completed review is available here yet." : "Nothing has been filed here yet."}));
    const generation = tell.boardGeneration;
    const cards = node("div", {class: "tell-cards"});
    const limit = tell.board === "daily-review" ? 1 : 3;
    for (const post of posts.slice(0,limit)) cards.appendChild(postNode(post, tell.board, generation));
    section.appendChild(cards);
    if (posts.length > limit) {
      const earlier = node("details", {class:"tell-earlier",data:{tellDetail:"earlier-"+tell.board}}, [node("summary",{text:"Earlier notes · "+(posts.length-limit)})]);
      const olderCards = node("div",{class:"tell-cards"}); earlier.appendChild(olderCards);
      earlier.addEventListener("toggle",()=>{if(earlier.open && !olderCards.children.length){
        for(const post of posts.slice(limit))olderCards.appendChild(postNode(post,tell.board,generation));
      }});
      section.appendChild(earlier);
    }
    host.appendChild(section);
    if(discovery)host.appendChild(discovery);
    if (failures.length) host.appendChild(reviewHistory(failures));
    observePosts(posts.slice(0,limit), tell.board, generation, section);
  }
  function observePosts(posts, boardId, generation, section) {
    if (!window.IntersectionObserver) return;
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting || entry.intersectionRatio < .35) continue;
        const post = posts.find((item) => item.id === entry.target.dataset.post);
        if (post && post.unread) void markSeen(post, boardId, generation, entry.target);
        observer.unobserve(entry.target);
      }
    }, {root: $("#viewTell"), threshold: [.35]});
    tell.observer = observer;
    for (const post of posts.filter((item) => item.unread)) {
      const article = section.querySelector('[data-post="' + CSS.escape(post.id) + '"]');
      if (article) observer.observe(article);
    }
  }
  async function markSeen(post, boardId, generation, article) {
    if (!post.unread || post.seeing) return;
    post.seeing = true;
    try {
      await api("/api/tell/posts/" + encodeURIComponent(post.id) + "/seen", {method: "POST", body: {}, absolute: true});
      post.unread = false;
      delete post.seeing;
      const board = (tell.summary.boards || []).find((entry) => entry.id === boardId);
      if (board && board.unread_count) board.unread_count -= 1;
      if (tell.summary.unread_count) tell.summary.unread_count -= 1;
      if (tell.board === boardId && tell.boardGeneration === generation && article && article.isConnected) {
        const badge = article.querySelector(".tell-new");
        if (badge) badge.remove();
        const button = article.querySelector(".tell-mark-read");
        if (button) button.remove();
        tell.contentSignature = JSON.stringify([tell.board, tell.summary.schedule, tell.summary.discovery, tell.boardPayload]);
      }
      updateNav();
    } catch (_) { post.seeing = false; /* A read marker never prevents reading the board. */ }
  }
  async function addReply(id, form) {
    const field = form.querySelector("textarea");
    const body = field.value.trim();
    if (!body) return;
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      await api("/api/tell/posts/" + encodeURIComponent(id) + "/replies", {method: "POST", body: {body}, absolute: true});
      field.value = "";
      tell.replyDrafts.delete(id);
      await loadBoard(true);
    } catch (error) {
      const note = node("p", {class: "tell-error", text: "Could not add comment: " + error.message});
      form.appendChild(note);
    } finally { button.disabled = false; }
  }
  async function setSchedule(schedule_enabled) {
    try {
      const result = await api("/api/tell/settings", {method: "PATCH", body: {schedule_enabled}, absolute: true});
      tell.summary.schedule = result.schedule || {...tell.summary.schedule, enabled: schedule_enabled};
      render(true);
    } catch (error) {
      const host = $("#tellBody");
      host.prepend(node("p", {class: "tell-error", text: "Could not update the schedule: " + error.message}));
    }
  }
  async function loadBoard(force) {
    if (!enabled()) return;
    const boardId = tell.board;
    const generation = tell.boardGeneration;
    try {
      const path = boardId === "activity" ? "/api/tell/activity" : "/api/tell/boards/" + encodeURIComponent(boardId);
      const payload = await api(path, {absolute: true});
      if (boardId !== tell.board || generation !== tell.boardGeneration) return;
      const signature = JSON.stringify(payload);
      if (force || JSON.stringify(tell.boardPayload) !== signature) {
        tell.boardPayload = payload;
        render(force);
      }
    } catch (error) {
      if (viewOpen() && boardId === tell.board && generation === tell.boardGeneration) {
        $("#tellBody").replaceChildren(node("p", {class: "tell-empty", text: "Tell is not available right now: " + error.message}));
      }
    }
  }
  async function refresh() {
    if (tell.loading || !pageVisible()) return;
    tell.loading = true;
    try {
      const summary = await api("/api/tell/summary", {absolute: true});
      const before = tell.summary;
      tell.summary = summary;
      const cursor = String((summary.activity || {}).cursor || "");
      if (!tell.baseline) {
        const remembered = readCursor();
        tell.baseline = true;
        tell.cursor = cursor || remembered;
        if (remembered && cursor && cursor !== remembered) announceActivity();
        if (cursor) saveCursor(cursor);
      } else if (cursor && cursor !== tell.cursor) {
        tell.cursor = cursor;
        saveCursor(cursor);
        announceActivity();
      }
      updateNav();
      if (viewOpen() && (!before || JSON.stringify(before) !== JSON.stringify(summary))) render(false);
      if (viewOpen()) await loadBoard(false);
    } catch (_) {
      // The console is deliberately unaffected by a missing or older Tell service.
      if (!tell.summary) { tell.summary = {enabled: false, boards: [], unread_count: 0}; updateNav(); if (viewOpen()) render(true); }
    } finally { tell.loading = false; }
  }
  async function switchBoard(id) {
    if (id === tell.board) return;
    tell.board = id;
    tell.boardGeneration += 1;
    tell.boardPayload = null;
    tell.contentSignature = "";
    render(true);
    await loadBoard(true);
  }
  function start() {
    void refresh();
    if (!tell.poll) tell.poll = window.setInterval(() => void refresh(), POLL_MS);
  }
  function open() {
    tell.activityNotice = false;
    tell.activityPulse = false;
    if (tell.pulseTimer) { clearTimeout(tell.pulseTimer); tell.pulseTimer = null; }
    updateNav();
    start();
  }
  document.addEventListener("visibilitychange", () => { if (pageVisible()) void refresh(); });
  window.__tell = {open, refresh, switchBoard, state: tell};
  // Summary polling belongs to the console shell, so new reviews can reach
  // the Tell navigation even while the person is reading a native session.
  start();
})();
