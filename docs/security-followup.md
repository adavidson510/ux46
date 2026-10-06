# Security review follow-up: authority without disabling the assistant

The initial review is shipped in alpha.40 through PR #44. Issues #36–#43 track a different class of work: the distinction between a browser session, a native coding agent and content imported from elsewhere.

## Product decision · October 5, 2026

UX46 is an access layer around the native agents the owner chooses. Preserve their configured capabilities and useful cross-room coordination. Fix defects UX46 adds; do not make separate OS accounts, credential services or agent sandboxes mandatory. Stronger separation is an optional deployment choice, not a prerequisite for using the application.

A trusted full-power agent using the owner's tools is expected behavior. An unrelated website triggering those tools through UX46 is a defect. Incoming reports must retain their source and must not be misrepresented as the owner's instructions. This distinction does not justify disabling an assistant's authorized work.

The previous proposal to require protected update and email services is withdrawn. The following directions narrow the remaining review; they are not claims that every issue is implemented or closed.

## #36: document trust and protect the access boundary

The default is a single-owner local application. Native agents retain the filesystem, network and computer-use access the owner grants them. A loopback address, CSRF token, JSON reporter field or browser click cannot prove that a human personally performed an action. An extra bearer token does not isolate a same-account agent that can read it or operate an authenticated browser.

Review actual UX46 ingress defects: cross-site requests, untrusted identity headers, remote exposure and unintended access by other OS accounts. Fix those without claiming protection against an unrestricted process trusted with the same OS account. Preserve local launch, command-line tools, private remote access, voice/media and recovery. Do not require a new login ceremony solely to relabel an already trusted local caller.

For owners who want stronger isolation, document separate accounts/hosts and credentials inaccessible to the agents. Explain the remaining computer-use and administrative access routes. Optional isolation should have its own migration checks; it is not a gate for ordinary correctness fixes.

## #37: make email Send reliable

Keep drafting and sending distinguishable. The existing Send action should dispatch the exact reviewed account, recipients, body and saved draft/source revision. Prevent duplicate submissions, consume dispatch intent atomically, preserve unknown-send outcomes and never replay an uncertain send. Do not add a second routine confirmation after Send.

These are workflow and delivery guarantees, not proof that a human clicked. A trusted agent may have browser or direct tool access under the owner's chosen permissions. Attribute actions honestly where provenance is available; a review hash proves freshness, not human identity. A separately protected sender is optional, not required for these improvements.

Morning briefs may summarize several threads, but an outgoing draft must use its own thread. Do not accidentally copy another thread's private material into a reply. Show a changed Reply-To recipient clearly in the existing review flow.

## #38: suggestions and memory

Preserve source identity, revision/digest and attribution. Changed evidence must not silently replace the evidence previously assessed. An agent's suggestion must not become a human instruction through a reporter field. Keep deliberately requested room handoffs useful. Automatic suggestion routing remains opt-in; its content is a report within the configured workflow, not new authority by itself.

## #39: automatic assistant updates

Distinguish human requests from automatic project reports in storage, display and handoff metadata. Preserve source room, time, delivery ID and coverage; suppress duplicate delivery and never blindly replay an unknown dispatch. Do not describe an automatic report as something the human just said.

Keep the assistant's conversational and coordination capabilities. A deterministic report feed or tool-free summarizer can be an optional delivery path when it improves latency, cost or owner preference. It is not a mandatory replacement for a tool-enabled assistant. The owner chooses what monitoring and actions are authorized; the contents of a received report do not expand that scope.

## #40: Hermes approvals

The installed Hermes `tui_gateway/server.py` implements `approval.respond` through `resolve_gateway_approval(session_key, choice, resolve_all=...)`. It does not consume `request_id`. `sudo.respond` and `secret.respond` are separate credential methods. Merely adding an ignored request ID would not fix the issue.

The follow-up implementation retains requests individually, expires only a matching credential request, preserves request-bound clarifications, and refuses command/sudo/secret approvals in UX46 with a direction to the native Hermes interface. Restoring UX46 command approval requires a verified upstream exact-request contract. This is a deliberate functional limit, not a claim that sending an extra field fixes the runtime.

## #41: routine filing

Implemented in the follow-up branch: keywords can classify mail, but cannot authorize archive/read actions. Eligibility comes from Gmail's promotion/social categories or an explicit owner quiet rule. UX46-generated labels cannot authorize a subsequent filing pass. Messages with attention needs, warnings, conflicting rules or a new unindexed reply stay in the inbox.

## #42 and #43

Visualization content needs an offline or explicitly vendored library set, with no general-purpose script CDN permission. Direct top-level rendering needs a stricter policy than the isolated interactive frame. Keep ordinary documents and generated UI useful; do not quietly break them by removing libraries without replacement and a visible compatibility check.

The correctness backlog remains separate: repair corrupt existing blobs on reupload; serve recording ranges without reconstructing the whole recording; preserve edited skills and reject destination symlinks; correct upload capability reporting; validate adapter hosts and scoped previews; sanitize client errors; remove consumed draft recovery entries; and make platform tests use their actual supported toolchain. These are not closed by shipping alpha.40.
