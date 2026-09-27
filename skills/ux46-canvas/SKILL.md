---
name: ux46-canvas
description: Portable ux46-canvas guidance for a locally owned UX46 installation.
metadata:
  version: "0.2.0"
---

Publish compact boards using the local configured board store and the
version-checked tools/ux46_boards.py contract. Inspect `--help` first. The
standalone source alpha does not yet expose the gateway's complete Canvas
editing flow; never claim a board is visible without checking the actual UI.
Use existing uploaded file IDs for images, not arbitrary filesystem paths.

Read [the Canvas authoring guide](../../docs/canvas.md). Default to what matters
now, relevant recent history, and the next concrete step. Honor a requested list
or other arrangement instead. Keep human choices on refresh; use actual sourced
measurements and preserve the distinction between planned, reported and checked.
The panel can widen or expand, but it must also read well at its normal width.
The current schema supports items, line/bar charts and uploaded images; don't
claim a general generative component catalog or live data binding exists yet.

For something produced in one reply, use the guide's `ux46-preview` fence rather
than replacing the saved room overview. Register generated files through the
installation's existing managed-file flow first, and use the returned real ID.
Preview supports PDF, image, chart, Markdown and configured isolated
visualization references. Human overrides stay in the saved room Canvas.
