/* Native usage and compact continuity. These reads never start a model. */
(() => {
  "use strict";
  let generation = 0, context = null;
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const button = (text, fn, cls = "ghost") => {
    const node = make("button", cls, text); node.type = "button";
    node.addEventListener("click", fn); return node;
  };
  const dialog = make("dialog", "desk-dialog efficiency-dialog");
  dialog.id = "efficiencyDialog"; dialog.setAttribute("aria-labelledby", "efficiencyTitle");
  const head = make("div", "desk-head");
  const heading = make("h2", "", "Usage & continuity"); heading.id = "efficiencyTitle";
  const close = button("", () => dialog.close(), "iconbtn closebtn"); close.setAttribute("aria-label", "Close usage and continuity");
  close.innerHTML = '<svg class="ic" aria-hidden="true" viewBox="0 0 20 20"><use href="#i-close"/></svg>';
  head.append(heading, close);
  const subtitle = make("p", "efficiency-subtitle"), nav = make("div", "efficiency-nav");
  const body = make("div", "efficiency-body"); body.setAttribute("aria-live", "polite");
  dialog.append(head, subtitle, nav, body); document.body.append(dialog);
  dialog.addEventListener("close", () => { generation++; });
  const number = (n) => Number.isFinite(n) ? new Intl.NumberFormat().format(n) : "Unavailable";
  const note = (text) => make("p", "settings-note", text);
  function metric(label, value, sub) {
    const card = make("div", "efficiency-metric");
    card.append(make("span", "", label), make("strong", "", number(value)));
    if (sub) card.append(make("small", "", sub)); return card;
  }
  function details(title, text) {
    const d = make("details", "efficiency-detail");
    d.append(make("summary", "", title), make("pre", "", text)); return d;
  }
  function failure(error) {
    body.replaceChildren(note(error.status === 404 || error.code === "forbidden_path"
      ? "This connector hasn’t received the efficiency update yet. Its native conversations still work."
      : error.message || "Could not read this information."));
  }
  async function show(mode) {
    context.mode = mode;
    const ctx = context, gen = ++generation;
    nav.replaceChildren();
    for (const [key, text] of [["usage", "Usage"], ["chapter", "Next chapter"], ["skills", "Skills basket"]]) {
      const b = button(text, () => void show(key), key === mode ? "primary" : "ghost");
      b.setAttribute("aria-pressed", String(key === mode)); nav.append(b);
    }
    body.replaceChildren(note("Reading…"));
    try {
      if (mode === "skills") {
        const result = await ctx.api("/api/skills");
        if (gen !== generation) return;
        body.replaceChildren(note("Small skills, loaded when useful. Browsing this basket doesn’t inject its contents into your conversations."));
        for (const skill of result.skills || []) {
          const card = make("section", "efficiency-skill");
          card.append(make("h3", "", skill.name), note(skill.description), note("Version " + (skill.version || "unversioned")));
          const read = button("Read skill", async () => {
            read.disabled = true;
            try {
              const content = await ctx.api("/api/skills/" + encodeURIComponent(skill.id || skill.name));
              if (gen !== generation) return;
              const d = details("Skill instructions", content.content || content.text || "No instructions returned.");
              d.open = true; card.append(d); read.remove();
            } catch (error) { card.append(note(error.message)); read.disabled = false; }
          });
          card.append(read); body.append(card);
        }
        body.append(note("Ask an agent to pull the relevant skill from this basket. Installation uses that agent’s own host and native skill folder; it doesn’t change its identity or permissions."));
        return;
      }
      if (!ctx.room) { body.replaceChildren(note("Open a conversation first.")); return; }
      if (mode === "chapter") {
        const preview = await ctx.api("/api/room/" + ctx.room + "/chapter-preview");
        if (gen !== generation) return;
        const entry = preview.chapter || preview;
        const brief = entry.brief || "";
        body.replaceChildren(make("h3", "", "A fresh chapter. The same project."),
          note("Keep the tab name, icon and Canvas. Earlier conversations remain available. Codex starts with this compact handoff and reads deeper only when needed."));
        body.append(details("What carries forward", typeof brief === "string" ? brief : JSON.stringify(brief, null, 2)));
        body.append(note((entry.size_chars || brief.length) + " handoff characters. This is not the total native startup context."));
        const feedback = note(preview.reason || preview.message || "");
        feedback.setAttribute("role", "status");
        feedback.classList.add("chapter-feedback");
        feedback.hidden = !feedback.textContent;
        body.append(feedback);
        body.append(note("This uses saved project records, not an automatic summary of the conversation. If recent work or decisions are missing, ask the current agent to prepare a fresh handoff first."));
        const acts = make("div", "efficiency-actions");
        const start = async (command) => {
          for (const control of acts.querySelectorAll("button")) control.disabled = true;
          feedback.hidden = false; feedback.textContent = "Starting the next chapter…";
          try {
            const result = await ctx.start(command);
            if (gen !== generation) return;
            if (!result?.ok) throw new Error(result?.message || "The chapter did not start. No success was confirmed.");
            dialog.close();
            document.querySelector("#draft")?.focus({preventScroll: true});
          } catch (error) {
            if (gen !== generation) return;
            feedback.textContent = error.message;
            for (const control of acts.querySelectorAll("button")) control.disabled = false;
          }
        };
        const roll = button("Next chapter in this tab", () => start("/new"), "primary");
        const parallel = button("Open alongside", () => start("/new parallel"));
        roll.disabled = parallel.disabled = preview.eligible === false;
        acts.append(roll, parallel, button("Start blank", () => start("/new blank")));
        body.append(acts, note("A running turn, active goal or unsettled message must be resolved before rolling forward. Goals are not copied into a second runner."));
        const prepare = button("Ask agent to prepare a fresh handoff", async () => {
          prepare.disabled = true;
          try { await ctx.prepare(); if (gen === generation) dialog.close(); }
          catch (error) { if (gen === generation) {feedback.hidden = false; feedback.textContent = error.message; prepare.disabled = false;} }
        });
        prepare.disabled = preview.eligible === false;
        body.append(prepare, note("Uses one request to this conversation’s current agent and its normal token allowance. When it finishes, reopen Next chapter to inspect the saved handoff. Nothing rolls forward automatically."),
          button("Refresh handoff & readiness", () => void show("chapter")));
        if (ctx.parents.length) {
          body.append(make("h3", "", "Earlier chapters"));
          for (const previous of ctx.parents) body.append(button((previous.label || "Earlier conversation") + (previous.at ? " · " + previous.at.slice(0, 10) : ""), () => {dialog.close(); void ctx.history(previous.room);}, "linkbtn"));
        }
        return;
      }
      const day = new Date().toISOString().slice(0, 10);
      const data = await ctx.api("/api/room/" + ctx.room + "/usage?day=" + day);
      if (gen !== generation) return;
      const totals = data.usage || data.totals || {};
      body.replaceChildren(make("h3", "", "This conversation · " + day + " UTC"));
      if (data.pending || data.incremental?.pending) body.append(note("Indexing native receipts. Values below cover the part read so far. Refresh this view in a moment; no model is running for this."));
      const grid = make("div", "efficiency-metrics");
      grid.append(metric("Input", totals.input_tokens, "Includes cached input"),
        metric("Cached input", totals.cached_input_tokens, "A subset of input"),
        metric("Output", totals.output_tokens, "Includes reasoning where reported"),
        metric("Reasoning", totals.reasoning_output_tokens, "A subset of output"));
      body.append(grid, note("Native receipt totals are observations, not an account bill. Cached tokens still have a cost; unknown usage is never treated as zero."));
      if (Number.isFinite(totals.latest_input_tokens)) body.append(note("Latest model input: " + number(totals.latest_input_tokens) + " tokens" + (totals.latest_model ? " · " + totals.latest_model : "")));
      if (data.coverage) body.append(details("Coverage & measurement", typeof data.coverage === "string" ? data.coverage : JSON.stringify(data.coverage, null, 2)));
      if (data.annotations?.length) body.append(details("Compaction & counter changes", data.annotations.map((a) => typeof a === "string" ? a : JSON.stringify(a)).join("\n")));
      const usageActions = make("div", "efficiency-actions");
      usageActions.append(button("Refresh receipts", () => void show("usage")),
        button("Project & background usage", async (event) => {
          const control = event.currentTarget; control.disabled = true;
          try {
            const result = await ctx.api("/api/usage?day=" + day + "&project=" + encodeURIComponent(ctx.room.split("/")[0]));
            if (gen !== generation) return;
            body.append(make("h3", "", "Measured project conversations"),
              note((result.native_threads || []).length + " indexed native conversations. Only opened usage records are included; this is not a full account total."));
            const projectGrid = make("div", "efficiency-metrics");
            projectGrid.append(metric("Project input", result.usage?.input_tokens), metric("Project output", result.usage?.output_tokens));
            body.append(projectGrid);
            const background = result.background || {};
            body.append(make("h3", "", "Tell daily reviews · separate from project usage"));
            if (background.coverage === "unavailable") body.append(note("No Tell review receipt directory is available on this host."));
            else {
              body.append(note(number(background.usage?.receipts) + " receipts · " + number(background.usage?.recorded_usage_receipts) + " with reported usage · " + number(background.unknown_cost_receipts) + " with unknown usage"));
              const grid = make("div", "efficiency-metrics");
              grid.append(metric("Tell input", background.usage?.input_tokens), metric("Tell output", background.usage?.output_tokens)); body.append(grid);
            }
          } catch (error) { if (gen === generation) { body.append(note(error.message)); control.disabled = false; } }
        }));
      body.append(usageActions);
      body.append(note("Codex manages native compaction. Start another chapter at a useful milestone; there is no automatic reset timer or monitoring agent."));
    } catch (error) { if (gen === generation) failure(error); }
  }
  window.UX46Efficiency = {open(ctx) {
    context = ctx; subtitle.textContent = ctx.label + " · " + ctx.agentLabel;
    if (!dialog.open) dialog.showModal(); void show(ctx.mode || "usage");
  }};
})();
