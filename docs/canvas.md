# Give each room a useful view

For a draft you want to edit and save, use [Canvas scratchpads and content
popouts](content.md). They sit alongside the compact overview and keep their own
revision history; Notes and the overview remain separate.

Canvas should help someone understand the work without reading the conversation
again. Start with **what matters now**, then enough recent history to explain
it, then the next concrete step. Those are questions to answer, not mandatory
columns. Keep a shopping list a shopping list when that is what the person wants.

The right panel has three sizes: its normal width, a width you choose by dragging
the left edge, and **Expand** for the whole window. **Restore** or Escape returns
from Expand to the previous size. **Reset** restores the normal width. Width is
saved on this browser; the board itself still belongs to its agent and room.
Keyboard users can focus the divider and use Left/Right, or Home/End for its
smallest/largest width. Expanding preserves open details and unfinished edits.

## A room overview and a preview from a reply

**Canvas** is the saved room overview. **Preview** is something the agent made
or chose to show in a particular reply. Opening a preview never replaces the
saved overview. Back to room Canvas returns to it, including unfinished edits.
Switching rooms clears the temporary preview; its original reply still contains
an Open preview button. Preview is not synchronized as another device's chosen
panel or written to the board store.

An agent can include a complete `ux46-preview` JSON fence in its reply:

````markdown
```ux46-preview
{"type":"pdf","title":"Release review","file_id":"ACTUAL_MANAGED_FILE_ID"}
```
````

Use a real managed upload ID from this room, with `file_agent` when its owning
adapter differs from the current agent. A local path in prose does not register
a file. PDFs use the browser's built-in viewer and have a Download fallback.
Only PDF-signature bytes from the existing authenticated managed-file download
route enter that viewer. Images use the managed raster preview route.

Other supported descriptors:

- `image`: the same file fields as `pdf`.
- `chart`: a `chart` object using the existing line/bar schema.
- `markdown`: a `text` string, up to 50,000 characters, rendered with UX46's
  existing text-safe Markdown renderer.
- `visualization`: a `path` to an HTML fragment within the gateway's explicitly
  configured visualization directory. The existing sandbox and network
  restrictions still apply; this is not an arbitrary website iframe.

Titles are required and limited to 160 characters. Invalid or incomplete
descriptors remain visible as ordinary code. Use a preview only for something
that benefits from its own view; do not wrap every reply in one.

A new final answer can open its first declared preview on desktop when the
person is following the conversation. Reading earlier messages, editing the
Canvas, focus mode, an open dialog, or an already open Preview prevents that
automatic switch. On phones, use the reply's Open preview button. Old history
does not automatically reopen previews. There is no model call to render one.

**History** combines the former Turns and Updates buttons. It starts with both,
searches native history, and lets the reader narrow to either while keeping the
search text. Opening an older result still jumps to its original native item.

## Choose the content for the job

| Room | Lead with | Recent history | Next |
| --- | --- | --- | --- |
| Research or analysis | Current question and comparison | Observed measurements with period and units | Next observation or decision |
| Building an app | Usable result and what still needs checking | Changes that affected the result | Next test or delivery |
| Writing | Current draft or outline | Published work or accepted edits | Next editorial decision |
| Planning | Today's itinerary or useful list | Decisions already made | Upcoming dates and unresolved choices |
| Email | Messages needing a reply or decision | Handled messages | Drafts to review |

These are authoring patterns. The current board supports sections, items, small
line/bar charts and uploaded images. It does **not** yet provide a general table,
timeline, live mail view or arbitrary interactive component renderer. Existing
Current result links can take the reader to a richer artifact. An email summary
does not connect email data or authorize a send.

Use a chart only when its shape answers a question that a number cannot. Show
units, the period and the source beside it. Separate observations from estimates
and plans. A finished analysis is not proof of a successful business outcome.
Missing data should look missing, not like a zero measurement. Existing chart
values are exact; the plot's axis uses compact notation for large numbers.

Give the important label enough words to mean something. It wraps in the narrow
panel, with its value underneath; wider panels place them beside each other.
Essential facts must remain visible without opening the details.

## Keep the view dependable

Preserve human choices and edits. Update after a meaningful change, using the
current room's existing evidence and version-checked board contract. Don't wake
an agent just to repaint a status board. Where a project already has structured
data, a deterministic projection can refresh measurements without regenerating
the layout. Mark a dated snapshot as a snapshot; a save timestamp is not proof
the underlying information is current.

A future component catalog can extend these patterns while preserving the same
room ownership, familiar controls and explicit action boundaries. This release
improves space, previews and readability; it does not add that catalog or data bindings.
