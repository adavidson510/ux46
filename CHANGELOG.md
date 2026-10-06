# UX46 change history

Each release records what changed for the person using UX46. Older entries describe
that release; later entries may replace its behavior. Optional integrations still
require their own setup. These public versions do not identify which version a
customized installation is running.

## v0.2.0-alpha.40 · October 5, 2026

- **Security and reliability fixes from the October review.** Tightens local adapter access, browser-origin checks, file downloads, queued-message cancellation, shared-store paths and installer verification. See [review #44](https://github.com/adavidson510/ux46/pull/44) and [tracking #8](https://github.com/adavidson510/ux46/issues/8).
- **Remote SSH connections use a private socket.** Existing `local_port` settings remain accepted but no longer open a local TCP port. Agents still run on their own hosts.
- **Safer downloads and attribution.** HTML, JavaScript, SVG and XML attachments download as binary files; agents retain their original declared type. Agent-written handoffs and memory claims are labelled as agent reports. Email drafts identify replies addressed somewhere other than the original sender.
- **After updating:** restart the updated UX46 service and adapters when their active turns have finished, then refresh browser pages. Merging source or refreshing a page alone does not activate backend fixes. Existing installs and remote hosts need their own update.
- **Checked:** 113 browser journeys; targeted security regression checks; an actual Mac installer run using the private Python bootstrap; and a read-only connection to a configured remote agent through the new socket transport. No test message was sent to an agent or email recipient. Live native model execution was not exercised by these checks.
- **Still open:** local process/agent identity and authorization design, plus the separate follow-up backlog in issues #36–#43. This release does not claim to isolate an unrestricted agent from the OS account it runs under.

## v0.2.0-alpha.39 · October 5, 2026

- **One conversation picker on phones.** The current name and open-conversation count share one button. Tap it to search or switch conversations, rename a tab, or close one. The second dropdown is gone.
- **A readable version history.** Settings → What’s new opens this changelog. Earlier releases are included below, with links to their original notes.
- **After updating:** refresh open UX46 pages to load the new controls. This UI change does not require restarting an agent.
- **Checked:** phone and desktop picker journeys, search, rename/close access, and shared-desktop synchronization in browser fixtures. Physical phone feedback remains pending.

## v0.2.0-alpha.38 · 2026-10-05

Fix New conversation for Claude Code: recognize the native room ID receipt, close the dialog, and open the created conversation ready for its first message.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.38)

## v0.2.0-alpha.37 · 2026-10-05

Phone listening controls now stay tucked into the Listening button instead of covering the conversation or composer. Tap Listening to open controls; tap × or outside to hide them while audio continues. Desktop docking and independent listening windows are retained.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.37)

## v0.2.0-alpha.36 · 2026-10-04

Fix response audio getting stuck on Loading on mobile. Play stays available when a browser preloads only metadata; autoplay refusal asks for a tap, and stalled starts offer Reload audio.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.36)

## v0.2.0-alpha.35 · 2026-10-02

Signals now presents deliberate suggestions together, keeps old automatic findings in an archive, and never assigns room work merely because a post cites that room. Discuss prepares a message without replacing an unfinished draft. Reviewed suggestions stay in history.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.35)

## v0.2.0-alpha.34 · 2026-10-02

The assistant can find and contact any available room across configured agents. The watched-room selection now controls background updates only; contacting another room does not subscribe to unrelated updates. Additional-agent queries preserve their query parameters.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.34)

## v0.2.0-alpha.33 · 2026-10-02

The assistant can relay an explicit human request to a configured specialist room and bring its reply back. It verifies the current human input, binds the exact native destination, prevents duplicate sends, and matches replies to the accepted turn. Automatic source packets cannot originate handoffs; unknown delivery is inspected without replay.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.33)

## v0.2.0-alpha.32 · 2026-10-02

The assistant can now check existing Google email and calendar connections on request. A bounded read helper reports the accounts checked, calendar dates and time zones, partial coverage, and failures. It does not send mail, mark it read, edit calendars, or create scheduled reminders.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.32)

## v0.2.0-alpha.31 · 2026-10-02

Desktop Concierge alpha adds a dedicated conversation feed, selected-room monitoring, editable focus, pause/start controls and one-utterance Talk. It reuses Listen and its independent player.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.31)

## v0.2.0-alpha.30 · 2026-10-02

Listen can now dock beside its button or pop out into an independent window. The player carries the current response, playback position and queued replies between the two views, and keeps listening if the original workspace closes.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.30)

## v0.2.0-alpha.29 · 2026-10-01

The /usage command now opens account allowance, reset times and credit availability alongside separate conversation token receipts. It works while an agent is running and does not send a model prompt or redeem resets.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.29)

## v0.2.0-alpha.28 · 2026-10-01

Fix speech reporting that a visible reply is missing from native history. Voice now resolves message IDs from the same paged history as the conversation, rather than a different full-history projection. Remote lookup uses that same route.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.28)

## v0.2.0-alpha.27 · 2026-10-01

Session display names now take precedence over older names copied into saved desktop tabs. Renaming clears those copied names, so reset and reload work across devices. The Rename button is highlighted, shows Saving while working, and retains your text with a visible error if saving fails.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.27)

## v0.2.0-alpha.26 · 2026-10-01

Choose Listen in a conversation to hear new commentary and final answers in order. Tool activity is skipped. The feed stays pinned while you visit other rooms, with volume, mute, skip and stop controls. One-off response playback remains available.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.26)

## v0.2.0-alpha.25 · 2026-10-01

Resume now closes the goal menu and keeps it closed after a successful response or queued request. Goal state still updates; a failed Resume shows its error with the menu available to retry.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.25)

## v0.2.0-alpha.24 · 2026-09-30

Canvas now includes saved Markdown scratchpads: edit, preview, save, recover older revisions, and open the same document in a focused popout window. Agent changes and browser edits use revision checks, so a stale save cannot silently replace newer work. Chart snapshots use the same window with source labels and inspectable values.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.24)

## v0.2.0-alpha.23 · 2026-09-30

Canvas now includes saved Markdown scratchpads: edit, preview, save, recover older revisions, and open the same document in a focused popout window. Agent changes and browser edits use revision checks, so a stale save cannot silently replace newer work. Chart snapshots use the same window with source labels and inspectable values.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.23)

## v0.2.0-alpha.22 · 2026-09-29

Record audio without sending a message to an agent. Keep using the workspace while the microphone records; Pause and Stop remain visible. Saved audio can be named, filed under a project, transcribed on a configured local host, and searched. Recordings start Unfiled. Session Vault indexes recording metadata and reviewed summaries separately from native conversations.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.22)

## v0.2.0-alpha.21 · 2026-09-29

Exploration conversations now have a built-in compass instead of EX initials. It appears automatically in new installs and existing exploration rooms, while custom room icons and uploaded project logos keep precedence. Resetting a custom room icon restores the default.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.21)

## v0.2.0-alpha.20 · 2026-09-28

Desktop 1 now keeps tab closures consistent across devices and browser windows.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.20)

## v0.2.0-alpha.19 · 2026-09-27

Open conversations now use stable icon tabs instead of shrinking names and controls as the strip fills.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.19)

## v0.2.0-alpha.18 · 2026-09-27

Canvas can now widen or fill the window without losing your place. A room keeps its saved overview while an agent reply can open a separate temporary preview.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.18)

## v0.2.0-alpha.17 · 2026-09-25

History reads recover from a dropped connection with one bounded retry. After a long absence, a conversation following the newest messages opens a fresh native page; readers of older text keep their place and a Read latest action. Connection failures and a long catch-up have distinct explanations.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.17)

## v0.2.0-alpha.16 · 2026-09-23

Queued commands now use compact rows without a header. Successful and cancelled requests disappear automatically; failed or uncertain outcomes stay visible. Expand a pending row to see its wait reason, elapsed time and last check. Consecutive duplicate waiting requests are coalesced across windows while preserving command order.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.16)

## v0.2.0-alpha.15 · 2026-09-22

Login refresh results now count conversations separately from the background session-list connection. A typical result reads “9 conversations refreshed · 3 not running,” followed by an explanation that non-running conversations use the saved login when opened. Waiting and uncertain results remain visible. The dialog also distinguishes refreshing a login from reloading interface updates.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.15)

## v0.2.0-alpha.14 · 2026-09-21

Canvas now keeps the room's working content first. Linked ideas are collapsed below it, and expanding them cannot squeeze the Canvas out of view. Empty result placeholders no longer occupy the top of the drawer.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.14)

## v0.2.0-alpha.13 · 2026-09-21

The email brief notification now has a mobile-friendly dismiss button. Closing it remembers that brief in this browser, including after a page refresh; a newly prepared brief can notify again. Dismissing the notice does not mark the brief or Gmail messages read.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.13)

## v0.2.0-alpha.12 · 2026-09-21

Signals opens with three recent ideas. Older notes, project roundups and settings are folded away. Plain-language explanations keep the original notes and sources available and expire when their source changes. Explore this explains the room-review workflow without sending a message or starting work, and opens the room the person explicitly chose.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.12)

## v0.2.0-alpha.11 · 2026-09-21

Command Center is removed from navigation and Attention’s footer. Attention remains the workspace overview; existing conversations, runtime requests and Canvas stay available. Older Command Center navigation targets return to the conversation.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.11)

## v0.2.0-alpha.10 · 2026-09-21

Email has its own morning brief, full-thread reader, editable drafts with explicit Send, and reversible Gmail filing. Optional account configuration and an OS scheduler are required; fresh installations keep email disabled.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.10)

## v0.2.0-alpha.9 · 2026-09-21

Slash commands that need an idle session can now wait instead of asking you to stop your agent.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.9)

## v0.2.0-alpha.8 · 2026-09-21

A small-pilot release with working desktop management.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.8)

## v0.2.0-alpha.7 · 2026-09-20

A workspace you can recover and safely make your own.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.7)

## v0.2.0-alpha.6 · 2026-09-20

Conversation views now recover missed updates and keep delayed history replies in their owning room and agent.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.6)

## v0.2.0-alpha.5 · 2026-09-15

Choose a Codex agent’s access default in Room settings → Connection. Fresh installs start with project access; full OS-account access is an explicit choice. Existing installations retain their prior defaults.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.5)

## v0.2.0-alpha.4 · 2026-09-15

UX46 now leads with the person’s need: tell your AI what would help, try it, and make the workspace yours as you go. Building a personal agent is a possible outcome, not a prerequisite.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.4)

## v0.2.0-alpha.3 · 2026-09-15

UX46 now has a guided path from installation to your first local customization, with a new illustrated README and a code tour for people learning while they build with AI.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.3)

## v0.2.0-alpha.2 · 2026-09-15

Give the same curl command to a person or their AI.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.2)

## v0.2.0-alpha.1 · 2026-09-15

UX46 now has human and AI installation paths for a fresh, locally owned workspace.

[Original release notes](https://github.com/adavidson510/ux46/releases/tag/v0.2.0-alpha.1)
