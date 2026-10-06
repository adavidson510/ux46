# Keep useful experience, cut repeated review

Constellation is a bounded library of sourced lessons. Search when past experience
could change the current task. It can return nothing; matching a single incidental
word is not sufficient evidence of relevance. Briefs show direct matches, with
failure warnings retained. Exact lessons, sources and connections remain available
for inspection. Rendering and retrieval do not invoke a model; reading and applying
results still uses the active conversation's context and effort.

The limit is 100 **active** lessons. Retired and superseded records keep their exact
history without occupying active slots. Reactivation checks the same limit. Review
obsolete guidance rather than deleting rare useful safeguards or silently raising
the limit. The interface distinguishes active and archived records and calls
outcomes reported outcomes, not measured savings.

Attribution follows the writer. Only the workspace (the human) records a lesson
as `human-confirmed`; a lesson captured through the agent CLI or a granted agent
token is `agent-asserted`. When an agent reports a human direction
(`origin: human-direction` or `evidence: explicit-direction`), the claim is kept
under `asserted` but the lesson is stored as an agent observation and recall warns
that the human has not confirmed it. Saving the lesson from the workspace confirms
it; the human may revise or retire any lesson, including one an agent captured.
All local agents still share one principal, so one agent can revise another's
lesson; per-room agent identities are a separate design change.

Signals offers one suggestions view with previous categories retained on the
original cards. Previous automatic findings are folded into Earlier notes; daily
reports remain in Roundup archive. Reading a finding never assigns a room work.
Choose a room explicitly, then Discuss prepares a message without sending it or
replacing an unfinished draft. The room handles assessment and evidence in its
normal work. Covered and not-applicable suggestions move into Reviewed; new source
evidence can make them relevant for review again.

Tell's addressed messaging is independent of this interface. Local installations
choose whether to run an optional review scheduler; no scheduler is added by these
changes. Owners can pause nightly synthesis and automatic check-ins while retaining
archives and deliberate contributions.
Email remains a separate workflow.

Evaluate reuse on actual decisions: what would have happened before recall, what
changed after it, and what was observed. Record available costs, not invented
savings. A source consulted, an experiment chosen, a test passed and a useful human
result are different observations. Use existing feedback instead of another inbox
or a model that watches the other models.
