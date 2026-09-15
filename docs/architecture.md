# Architecture

UX46 wraps native agent runtimes rather than providing another reasoning loop.
The browser uses a loopback Python service. Runtime adapters handle native
session ownership and input/output; they do not replace provider authentication.

| Area | Source |
| --- | --- |
| Installer and local setup | install.sh, tools/ux46_setup.py |
| Standalone init/run | tools/ux46_local.py |
| Browser workspace | app/console/ |
| Codex console and workers | tools/atlas_console.py, atlas_native.py, atlas_workers.py |
| Claude adapter | tools/atlas_claude.py |
| Project registry and portable sessions | tools/session_vault.py, atlas.py |
| Learning, retrieval and feedback | tools/constellation_store.py, constellation_learning.py |
| Human workspace projections | tools/ux46_workspace_api.py |
| Optional remote agents | tools/atlas_remote.py |
| Optional private access gateway | tools/ux46_access_gateway.py |

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
See the Constellation skill for local access.

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
