[← UX46](../README.md) · [Code tour](code-tour.md) · **Architecture** · [AI setup](ai-install.md)

# The pieces and their boundaries

UX46 wraps native agent runtimes rather than providing another reasoning loop.
The browser uses a loopback Python service. Runtime adapters handle native
session ownership and input/output; they do not replace provider authentication.

| Area | Source |
| --- | --- |
| Installer and local setup | [install.sh](../install.sh), [ux46_setup.py](../tools/ux46_setup.py) |
| Installed source recovery and lifecycle | [ux46_manage.py](../tools/ux46_manage.py) |
| Standalone init/run | [ux46_local.py](../tools/ux46_local.py) |
| Browser workspace | [app/console/](../app/console/) |
| Codex console and workers | [atlas_console.py](../tools/atlas_console.py), [atlas_native.py](../tools/atlas_native.py), [atlas_workers.py](../tools/atlas_workers.py) |
| Claude adapter | [atlas_claude.py](../tools/atlas_claude.py) |
| Project registry and portable sessions | [session_vault.py](../tools/session_vault.py), [atlas.py](../tools/atlas.py) |
| Learning, retrieval and feedback | [constellation_store.py](../tools/constellation_store.py), [constellation_learning.py](../tools/constellation_learning.py) |
| Human workspace projections | [ux46_workspace_api.py](../tools/ux46_workspace_api.py) |
| Optional remote agents | [atlas_remote.py](../tools/atlas_remote.py) |
| Optional private access gateway | [ux46_access_gateway.py](../tools/ux46_access_gateway.py) |
| Doctor and independent recovery | [ux46_doctor.py](../tools/ux46_doctor.py), [ux46_recovery.py](../tools/ux46_recovery.py), [recovery guide](recovery.md) |
| Bounded event pages and restart detection | [ux46_events.py](../tools/ux46_events.py) |

Event replies acknowledge only the returned page, with a continuation flag for
another page. A process identity (`epoch`) and explicit gap flag tell the browser
when it must reread state. Events invalidate a view; they are not a transcript or
proof that a conversation is current. Older adapters without this contract get
snapshot reconciliation on every poll. New adapters also get periodic snapshots,
and failed reads retain the last-known content with a visible freshness notice.

History and room reads belong to the selected agent, room and read generation.
Late replies cannot replace a newer read or enter another conversation. History
catch-up reads back to the last observed item, up to 20 pages of 40 items. Pages
are never spliced across a gap that could not be verified. These
reads do not reconnect workers, resend input, or overwrite a draft. A dropped
history read gets one bounded retry. When the gap exceeds the catch-up window,
a person following the newest messages gets a fresh native page with earlier
history still reachable. Someone reading older text keeps their place and a
Read latest action. A missing attachment closes each owned file handle once;
it must not close a handle another request has just opened.

Commands that need an idle session (`/model`, `/effort`, `/compact`, `/new`,
`/refresh`, `/goal resume` and `/goal clear`) can wait in the browser's saved
command queue. It binds each action to its original agent, room and native
thread, shows the wait reason and last check, and preserves later drafts and
navigation. Successful and cancelled commands disappear automatically; failures
and unknown outcomes remain visible. Repeated identical waiting requests are
coalesced without changing the order of intervening commands. Goal continuation
and the current reply’s activity are explained separately. Read-only commands, steering and goal pause keep their immediate
behavior. A new conversation created in the background offers an explicit Open
action. Native validation and ownership checks still apply at dispatch.

This queue is local to the browser, separate from the server's ordinary message
queue. Keep UX46 open to execute waiting commands; if every window is closed,
queued entries wait until reopening. Web Locks prevent two windows in the same
browser from dispatching an entry twice. A dispatched action whose outcome was
lost is shown as unknown and is never automatically repeated; later commands
for that native thread wait until the unknown entry is reviewed and dismissed.
Approvals, unreadable state and changed native targets cannot authorize dispatch.
The queue does not require a native service restart or a model polling loop.

The standalone launcher separates source from private configuration and state.
The registry starts empty. Native metadata enriches explicitly filed sessions;
there is no sweep of historical transcripts during initialization.
The setup record in private `connection.json` gives an agent a small entrypoint
to this installation's tool commands and memory. Standalone Claude runs behind
an internal loopback adapter; the browser uses the same front origin. With no
provider selected, the workspace starts without a local runtime. Custom adapters
are registered explicitly and verified separately.

Constellation uses bounded SQLite queries and sourced, revisioned records.
An agent retrieves a short brief, follows specific evidence when useful, and
reports outcomes after reuse. It does not load the whole archive into context.
Human preferences, agent discoveries, and hypotheses need distinct attribution.
Low usefulness can demote or retire knowledge without deleting source history.
See the [Constellation skill](../skills/constellation/SKILL.md) for local access.

Shared learning, Tell, email and remote access require explicit configuration.
Tell's implementation is not bundled. Its adapter can connect to an installed
service. A failed remote connection is not permission to substitute another
agent or another knowledge store.

The standalone server checks loopback hosts; mutations additionally require
same-origin and a CSRF token. It is not a multi-user public Internet server.
The optional gateway has its own authentication boundary. Keep it separate
from runtime permissions and provider accounts.

Several older files retain the internal `atlas` naming for compatibility.
`app/static` and `atlas_ui.py` are legacy source, not the current launcher.

The installer keeps a persistent private transaction beside UX46_HOME. It records
release and archive evidence, exact paths, created-path ownership and setup phase.
The installed-base manifest records source hashes; reruns resume owned work and
refuse unrelated destinations. A copied controller supports open/stop and source
undo outside the editable tree. Source points exclude local caches and Git internals;
undo preserves prior source and carries local Git history forward. The special
workspace project stores portable records under private data while its native
working directory points at installed source.

An independently deployed access gateway can serve `/modules.js` with the UI.
When its upstream predates optional modules, `--modules-config /private/modules.json`
explicitly selects `email`, `tell`, and `constellation` using boolean values. The
settings endpoint remains authenticated. Without this option it forwards the
installed console’s settings; host preferences stay outside reusable source.

Room result/review and experiment controls use the private work.sqlite3 owner store;
Tell and Constellation retain their source records. See [work and learning](work-learning.md).
Email remains a separate workflow: [briefs, reviewed replies and reversible Gmail filing](email.md).
