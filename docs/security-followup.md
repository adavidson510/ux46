# Security review follow-up: authority without disabling the assistant

The initial review is shipped in alpha.40 through PR #44. Issues #36–#43 track a different class of work: the distinction between a browser session, a native coding agent and content imported from elsewhere.

## Decision being made

UX46 should keep direct conversation and explicitly requested cross-room coordination useful. Reading a room update is not permission to send email, change settings or start new work. A model prompt saying "do not use tools" is not an access boundary.

The proposed default is to preserve powerful coding rooms while isolating automatic update processing and the service that can send email. Full isolation of every coding room is a different product choice: its filesystem, credentials and network access must be constrained too. This choice is pending owner direction; the proposal below is not an implemented boundary.

## #36: separate transport access from authority

There are three distinct concerns:

1. Other local OS accounts must not acquire owner access from `/api/bootstrap`. Bind backend adapters to owner-only Unix sockets, or require a protected installation credential. The browser front remains accessible through a separately authenticated entry point. Local launch/open and independent recovery must use that entry point too.
2. Browser and agent requests need separate server-derived principals. JSON fields, free-text reporter names, a loopback address and a CSRF token cannot assert that a human authorized something. The access gateway must strip client-supplied identity/role headers and inject only an authenticated identity over the protected backend transport.
3. An unrestricted process under the service owner's OS account can read its credentials or modify its code/state. Another 0600 file or bearer token does not isolate that process. Strong protection from these agents requires a separate service account/host with agent-inaccessible credentials, or genuinely constrained agents. Administrator/root access to that service host remains outside the boundary.

Rollout must include browser login, command-line reads, private front-door access, local and SSH adapters, voice/media requests, recovery, reconnects, and explicit refusal of an old unprotected path. Do not silently migrate some routes and leave a bootstrap bypass elsewhere. Existing installations require migration; a source merge alone changes none of their running services.

## #37: sending email

Keep drafting and sending separate. A sender service owns the Gmail sending credential; coding rooms can request drafts, not obtain that credential. The existing review screen supplies the human action. It binds a one-time authorization to the exact account, recipients, body, saved revision and source-thread revision. Consume it atomically before dispatch, preserve unknown-send outcomes, and never replay an uncertain send.

A nonce from an unauthenticated loopback endpoint would not establish human intent. This depends on #36's authenticated browser principal and, for protection from powerful same-account agents, a real process/account boundary. Do not add a second routine confirmation after the existing Send button. A changed Reply-To recipient warrants an explicit exception review.

Morning briefs may summarize several threads, but an outgoing draft must be generated from its own thread. Content copied from a multi-thread summary must never become the body of a reply.

## #38: suggestions and memory

Store the source revision/digest selected by the human. Changed evidence becomes a visible proposal, not silently substituted text in another room's input. Agent-routed observations may appear as agent suggestions; they must not be attributed to the human or automatically injected as their instructions. Automatic checkpoint injection should remain off. An explicit request to discuss a suggestion can supply its quoted data to that room.

## #39: automatic assistant updates

Keep two paths:

- **Human conversation:** the ordinary assistant room can use its authorized read and coordination tools when asked.
- **Automatic reports:** collect changes deterministically, optionally summarize in an isolated tool-free process, and render/speak the resulting update as an attributed report. Do not submit it as a user turn to the tool-enabled assistant. Filtering the wording in a second model is not a substitute for removing that dispatch path.

The update store must retain source room, time, delivery ID and coverage. Display/listening must work without mutating native history. Unknown deliveries must not be replayed. Referencing an update during a later human turn is an explicit read; the update itself never authorizes a handoff or send.

## #40: Hermes approvals

The installed Hermes `tui_gateway/server.py` implements `approval.respond` through `resolve_gateway_approval(session_key, choice, resolve_all=...)`. It does not consume `request_id`. `sudo.respond` and `secret.respond` are separate credential methods. Merely adding an ignored request ID would not fix the issue.

The follow-up implementation retains requests individually, expires only a matching credential request, preserves request-bound clarifications, and refuses command/sudo/secret approvals in UX46 with a direction to the native Hermes interface. Restoring UX46 command approval requires a verified upstream exact-request contract. This is a deliberate functional limit, not a claim that sending an extra field fixes the runtime.

## #41: routine filing

Implemented in the follow-up branch: keywords can classify mail, but cannot authorize archive/read actions. Eligibility comes from Gmail's promotion/social categories or an explicit owner quiet rule. UX46-generated labels cannot authorize a subsequent filing pass. Messages with attention needs, warnings, conflicting rules or a new unindexed reply stay in the inbox.

## #42 and #43

Visualization content needs an offline or explicitly vendored library set, with no general-purpose script CDN permission. Direct top-level rendering needs a stricter policy than the isolated interactive frame. Keep ordinary documents and generated UI useful; do not quietly break them by removing libraries without replacement and a visible compatibility check.

The correctness backlog remains separate: repair corrupt existing blobs on reupload; serve recording ranges without reconstructing the whole recording; preserve edited skills and reject destination symlinks; correct upload capability reporting; validate adapter hosts and scoped previews; sanitize client errors; remove consumed draft recovery entries; and make platform tests use their actual supported toolchain. These are not closed by shipping alpha.40.
