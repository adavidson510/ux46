# Listen to a conversation

Open a conversation and choose **Listen** beside its model control. New progress
messages and final answers play aloud in order on this device. Tool activity,
reasoning and your own messages are skipped. Existing history is not replayed.
Text stays in the conversation. On desktop, the controls dock beside **Listen**;
click **Listening** to reveal the current response. On a phone, expand
**Response being read** in the player.

Listening stays with the conversation you chose. You can move around UX46
without changing that feed. Choose Listen in another conversation to switch.

- **Mute** pauses playback and holds incoming replies. **Unmute** continues.
- The volume slider adjusts this player. Device volume still applies.
- **Skip response** moves to the next reply; **Stop listening** clears the feed.
- The speaker button on each response still plays it once. Doing that pauses
  the live feed so the two voices do not overlap.

## A separate listening window

Choose **Pop out listening** (↗) beside the volume control. This opens a real
browser window with the full response and player controls. Move or resize it
using your operating system, including on another monitor or virtual desktop.
The browser decides whether popup requests appear as windows or tabs.

The reader moves into that window with its current position, volume, mute state
and waiting replies. It keeps listening if you close the main workspace.
**Dock back** returns it to the original Listen button. Closing the satellite
also returns it when that original workspace is still open. **Stop listening**
ends the feed and closes the satellite. If the original workspace is gone, the
satellite remains usable; Dock back is unavailable.

If a popup is blocked, playback stays where it was. If the browser blocks audio
after a handoff, choose **Play / retry**. Reloading ends that window’s reader;
reopen it from Listen in the main workspace.

Listening is local to this browser profile, never saved to your shared desktop.
Only one window in that profile owns live playback at a time. A phone or another
computer makes its own listening choice. No conversation is created, attached,
closed or prompted when you move the player.

## What the alpha checks

The reader checks the chosen conversation every five seconds without sending
a prompt or waking its agent. It waits for a message to finish before reading
it: either a later native item exists or the turn has ended. Speech uses the
host's existing local voice service; it makes no model calls. Voice must already
be configured on that host.

Keep the page open and the device awake. Browsers can suspend background pages
or block automatic audio. If that happens, **Play / retry** resumes the prepared
response. Locked-phone playback is not guaranteed. The local voice currently
reads up to 4,000 cleaned characters per response; a partial reading is labelled
with the spoken and total character counts. Full text remains available.

Failures retain the response for retry or skipping. The feed holds up to 100
waiting replies and checks at most 400 recent native items per catch-up. If it
falls further behind, it explains the gap and asks you to stop and restart from
now rather than silently claim it read everything.

## Voice lookup

Speech resolves the response ID through the same paged native history as the
conversation. It does not rely on a separate full-history view, which some
runtimes project differently.

Operators using the private access gateway can set `--voice-model-dir` to their
existing local voice model and `--voice-cache-dir` to a private cache directory.
This serves speech independently of running native workers, with the existing
identity, login, origin and CSRF checks. It downloads no model. The browser
sends only the message ID and chosen voice; the server retrieves the text.
