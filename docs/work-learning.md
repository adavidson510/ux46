# From a suggestion to a result

Signals/Tell holds discoveries and discussion. Constellation holds reusable
lessons with sources and reported outcomes. UX46 connects them to actual work;
Email has its own separate brief and actions.

Dismiss hides a signal across devices and keeps Restore available. Remind
tomorrow hides it for a day. Neither deletes the source or cancels an experiment.
Worth exploring records interest and lets you choose a room and explain why it
belongs there. Interest is never counted as a successful use.

The Result button beside the conversation opens the current result and room
review in Canvas. Keep an app link or article preview, its version, the changes
attributed to this work, actual checks and unresolved checks there. The UI does
not infer verification from the agent finishing, and does not claim an entire
repository diff belongs to that agent. Links open separately; arbitrary pages
are not embedded into the privileged workspace.

A room assessment says relevant, worth a test, already covered or not relevant,
with a reason. A chosen experiment records its baseline, hypothesis, check and
stopping point. Record helped, did not help, in use/awaiting outcome, or unknown
with evidence. A Constellation-origin experiment reports its exact applied
revision through the existing feedback API; delivery and interest remain separate.

## Where state lives

Tell keeps its posts; Constellation keeps its lessons. UX46's private work.sqlite3
owns personal visibility and room workflow references. This small ownership
refinement means the public workspace can use the workflow without bundling a
Tell server, and does not create competing copies of either knowledge store.
Stored source excerpts are dated references, not authority. Preserve significant
experiment decisions/results in the room's canonical Session Vault record.

Use the [work skill](../skills/ux46-work/SKILL.md) and tools/ux46_work.py for agent
reads and version-checked writes. The skill is in the normal UX46 basket.
An owner-configured work-review.json in ~/.config/ux46 can map reporters to
agent IDs and select the private work store:

```json
{"store":"/private/ux46/workspace/work.sqlite3","reporters":{"local":"local"}}
```

At a Session Vault checkpoint, an already-running room receives up to three
changed relevant sources, capped at 6 KB. It does not wake a model. The owner
must install the updated Vault implementation and work skill in that runtime;
a code copy does not prove a warm conversation adopted it. Review now can be
prepared in the room's composer without overwriting an existing draft.

Only material source changes or an explicit change to the room's objective
invalidate assessment. Comments and receipts do not produce review loops.
Repeated unchanged packets are suppressed. Pending review remains visible until
assessed; chosen work never starts merely because a packet arrived. Sleeping or
unconfigured rooms remain waiting. Routes are explicit, not a guessed broadcast.

Already tracked Tell sources refresh by bounded board reads. Configured shared
Constellation access remains subject to its existing grants. Cross-account or
cross-machine delivery is not implied by choosing a room name; install/configure
that room's receiver before claiming automatic adoption.
