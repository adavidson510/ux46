# Edit a draft beside the conversation

Open **Canvas → New scratchpad**, name it, and write Markdown. **Preview** shows
how it reads; **Save** stores it on your UX46 server. Your agent reads the last
saved version. Typing, previewing and saving do not start a model.

**Expand Canvas** gives you more room. **Pop out** opens the same document in a
separate browser window; browsers that prefer tabs can use **Open in a new tab**.
It stays on that document when you switch conversations. Closing either view
does not delete the document or close its conversation.

Save from either view. A clean reader picks up newer saves; unfinished typing
stays put. If two people or windows save different edits, the second save shows
the saved version for comparison and keeps your text. Compare first, then choose
which text to keep. Revision history retains the imported baseline and the last
50 saves. Loading a revision makes an unsaved working copy until you save again.

If saving fails, retry or **Download my text**. Browser recovery also keeps
unfinished edits on that device. It is not a cross-device backup: save before
moving to another device. Markdown supports the existing safe UX46 renderer;
embedded HTML stays text.

## Give the agent the same document

The agent must read the saved document before revising it. Do not reconstruct a
draft from conversation history. Each item has an ID, owning agent, room, kind
and revision. A popout is a view of that item, not a new agent conversation.

On the owning server, use the configured workspace-store directory (the one
passed to `ux46_workspace_api`, not the provider's native session store):

```sh
python3 tools/ux46_content.py --directory /path/to/workspace-store \
  --agent local --room example/draft
python3 tools/ux46_content.py --directory /path/to/workspace-store \
  --agent local --room example/draft --id RETURNED_CONTENT_ID
```

For a new draft, save this JSON to a file and pass `--file /path/to/action.json`:

```json
{"action":"create","kind":"markdown","title":"Working draft","payload":{"text":"# A draft\n\nStart here."},"reporter":"Agent"}
```

To update, read first and use its ID and revision in the action file:

```json
{"action":"save","id":"RETURNED_CONTENT_ID","base_revision":1,"title":"Working draft","payload":{"text":"# A draft\n\nRevised text."},"reporter":"Agent"}
```

A revision conflict refuses the write. Reread and reconcile the human's edits;
never blindly increment the revision and replay your old text. The CLI's agent
and room arguments determine scope. Every read/write checks that scope against
the item ID. This is scoped storage within one authenticated owner's workspace,
not a separate multi-user permission system. The same operations are exposed by
authenticated `GET /api/content/view` and CSRF-protected `POST /api/content/action`.

Once the save succeeds, include its real ID in an implemented content card:

````markdown
```ux46-content
{"id":"RETURNED_CONTENT_ID","title":"Working draft"}
```
````

The card provides **Open in Canvas** and **Open popout**. It resolves in the
current agent and room; optional `agent`/`room` fields must match. A direct click
avoids browser popup blocking. This fence never creates a document by itself.
For a saved item in another room, link to its explicitly scoped URL instead:
`/?content=ID&agent=AGENT&room=URL_ENCODED_ROOM`.

## Charts share the container

A chart uses the same create/read/save contract with `kind: "chart"` and:

```json
{"chart":{"type":"bar","labels":["Monday","Tuesday"],"values":[1,3],"unit":"points"},"source":"Synthetic example; two-day comparison","snapshot":true}
```

Supported charts are line/bar snapshots with 1–24 labels and finite values.
Include a meaningful source, observation period and units. The renderer shows
the source, snapshot label, save time and inspectable chart values, without
Markdown editing controls. The save time is not a claim of fresh data. This
does not connect brokers, schedule jobs, or fetch live market data.

Markdown is limited to 50,000 characters per revision. Managed PDFs and images
still use the existing [turn preview](canvas.md) rather than this editable
document store. Listening to saved documents is not part of this first slice.
