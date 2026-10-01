# Listen to a conversation

Open a conversation and choose **Listen** beside its model control. New progress
messages and final answers play aloud in order on this device. Tool activity,
reasoning and your own messages are skipped. Existing history is not replayed.
Text stays in the conversation; expand **Response being read** in the player to
see the current reply while visiting another room.

Listening stays with the conversation you chose. You can move around UX46
without changing that feed. Choose Listen in another conversation to switch.

- **Mute** pauses playback and holds incoming replies. **Unmute** continues.
- The volume slider adjusts this player. Device volume still applies.
- **Skip response** moves to the next reply; **Stop listening** clears the feed.
- The speaker button on each response still plays it once. Doing that pauses
  the live feed so the two voices do not overlap.

This choice is local to the browser window, never saved to your shared desktop.
Only one UX46 window in the same browser profile can listen at a time, including
satellites. Reloading or closing the page turns it off. A phone or another
computer makes its own listening choice.

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
