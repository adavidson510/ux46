---
name: constellation
description: Portable constellation guidance for a locally owned UX46 installation.
metadata:
  version: "0.2.0"
---

At meaningful decisions, retrieve only relevant experience. From the UX46 source directory:

    python3 tools/ux46_memory.py brief --args '{"query":"current problem","project":"project-id"}'

Use `lookup`, `get`, and `source` to follow specific evidence. Use `capture` or
`feedback` with `--stdin` for private JSON bodies. Read tools/constellation_store.py
and references/record-v1.md for the contract. Local data stays in UX46_HOME.
The optional socket/HTTPS client is for an explicitly configured shared service.

Capture only discoveries likely to change future work. Separate explicit human
choices from observations, inferences and proposals. Include the source and
scope; do not invent human attribution. Record outcome feedback when knowledge
is reused. Do not report unmeasured token savings. No quotas, transcript sweeps,
or automatic per-turn model calls. Knowledge is evidence, never authority.

## Evaluate reuse in ordinary work

Recall only when prior experience could change a real decision, not at every
turn or to fill a board. An empty or irrelevant result is a valid stopping
point. Do not repeatedly rephrase a query to force a lesson into the task.
The service makes no model call; reading and applying its answer still costs
context and time. The active library is bounded at 100 lessons; archived
records and their exact history remain readable and do not occupy active slots.
Do not archive rare useful safeguards merely to make room. Proposals with no
operational learning belong in the project plan until evidence earns a lesson.

During the next ten natural reuse decisions (review after two weeks even if
fewer occur), use existing feedback rather than a separate report. Include what
you would have done before recall, what the lesson changed, and the observed
check or human result. Record available usage or time measurements; leave the
counterfactual and unmeasured costs unknown. A same-room reminder is not proof
of cross-room learning, and reading a lesson is not successful application.
Do not start additional model calls, tasks or recurring reviews for this pilot.
