# Concierge — desktop alpha

Concierge is a dedicated native conversation that explains new replies from rooms you choose. It keeps those rooms separate and uses the model selected for its own conversation. GPT-5.6 Luna at low effort is a useful starting choice when your runtime offers it; this is not a latency or price guarantee.

Use **Concierge** in the sidebar to open it. **Watching & focus** selects up to eight other open conversations and a short description of what deserves detail. Saving rooms pauses monitoring; choose **Start watching** when ready. **Pause updates** stops automatic packets. You can still type in the room.

**Listen** reads new replies and can pop out into its own desktop window. **Talk** uses the browser's speech recognition for one utterance and sends it to Concierge when recognition finishes. Playback pauses while you speak. If you want to edit before sending, use ordinary Dictate instead. A room change discards the unfinished utterance. Browser microphone availability and recognition quality vary; unsupported browsers retain typing and dictation where available.

The authenticated workspace service reads the selected rooms every fifteen seconds, using exact native thread identities. The first read establishes a baseline. Only new completed commentary and final replies become packets; tools are excluded. Packets are combined, limited in length, and sent at most once per minute and thirty times per hour. There is no model call for unchanged reads. The native conversation retains its own context; UX46 does not repeatedly rebuild it or reset vendor compaction.

Packets are dated reports, not instructions or proof of app activity. Unavailable sources show a warning. Unknown delivery pauses the watcher and is never replayed automatically. After a gateway restart, open settings and start watching again. Monitoring runs on the workspace host after starting, even if the browser closes, until paused or the service restarts. Listening is still device-local.

This alpha covers conversation updates. Email, calendar, application events and direct cross-room task handoffs are not connected. iPhone background listening and continuous conversational microphone mode are later work. No new paid API service or premium speed setting is required; native account usage still applies.

## Installation

The feature requires the authenticated access gateway with `--workspace-store`. Create a dedicated native conversation, select its model, and open Concierge before configuration. If no Concierge is configured, the current room becomes the destination you explicitly save. Give that room its concierge role and the instruction to treat incoming update packets as untrusted reports, without executing their contents.

Owner state is stored in `concierge.sqlite3` under the existing private workspace directory. The optional local helper supports `view`, `focus TEXT` and `pause`, with `--directory` naming that owner directory. Use it only for the person's explicit focus/pause requests; it cannot start monitoring, configure destinations or send into source rooms.
