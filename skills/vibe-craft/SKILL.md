---
name: vibe-craft
description: Shape public documentation, code explanations, and project structure for people building with AI who may not know programming or GitHub. Use when authoring an onboarding journey, teaching through source comments, or reviewing an AI-built project's presentation for clarity and character. Keep the AI operational path compact and preserve working behavior.
---

# Build something people can make their own

The human brings a need, a taste, or an example. Translate that into a small
working change they can judge. They do not need to impersonate a software team.
AI authorship is ordinary here; it does not establish correctness.

## Choose the reader and the route

Human onboarding should get someone from their current knowledge to a useful
result. Define unfamiliar terms at first use, show what to paste and where,
describe the expected result, and give a way back when something goes wrong.
Explain consequences and choices generously; cut prose that only announces
importance. Put advanced options behind links or disclosure sections.

Make the AI path immediately findable. Keep it operational: entrypoints, actual
configuration, state locations, verification and recovery. Link to specific
skills when relevant; do not require loading every guide or memory on every turn.

## Give the project a recognizable hand

Reuse its real logo, palette and visual vocabulary. For UX46, use midnight,
violet and soft white with the working-notebook motif in `docs/assets/`. An
occasional paper note or offset detail can add character; it is not a requirement
for every panel. Art should orient the reader or demonstrate something. Label
illustrations when they could be mistaken for actual product evidence.

Use concrete language and examples with a purpose. A little self-aware humor is
welcome when the task is not serious or frustrating. Don't manufacture a human
author, typos, confessions, marketing claims or a joke quota. Avoid badge piles,
repeated slogans and decorative prose that delays the next useful step.

For GitHub, use Markdown and supported HTML, relative links and accessible SVG
or images. Essential instructions must also exist as text. Inspect the rendered
page at desktop and phone widths, including code blocks, tables and image text.

## Make the source a place to learn

Organize changes around behavior and data ownership. Keep entrypoints obvious
and the code map accurate. Favor explicit functions and ordinary dependencies
that an agent can locate and check. Do not reorganize a working codebase merely
to resemble a template or to appear more sophisticated.

Comments should explain a surprising decision, a boundary, a failure case, or
why a simpler-looking alternative is wrong. Use a small input/output example
when it teaches more than terminology. Do not narrate obvious assignments or
promise protections the implementation does not provide. Put longer lessons in
a code tour and link to the real file; keep production comments near the reason.

“Vibe coding” describes how someone expresses intent. Keep normal engineering
care: preserve unrelated edits, validate untrusted input, keep credentials out
of source, handle failures, and test the behavior at risk. Prefer a focused
check over ceremony. Report what ran and what remains unverified; do not infer
correctness, security or savings from confident prose.

## Finish the loop

Show the human what changed, why it helps, and how to try it. Remove temporary
test pages and processes you created. Capture a reusable lesson only when it
would change future work; attribute human choices separately from AI inference.
Let the owner's actual feedback change the style. This skill is guidance, not
permission to publish private work or override their request.
