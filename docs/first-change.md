[← UX46](../README.md) · [Install](install.md) · **Your first change** · [Code tour →](code-tour.md)

# Start with “I wish…”

You don't need to learn a programming language before you can describe a problem.
You do need a way to tell whether the change helped. That is where we start.

Let's use a small example: the selected tab is hard to spot. This is a good first
change because you can see the result immediately and undo it without moving
your conversations or changing how an agent runs.

## Give your AI the problem and a place to work

Open the workspace menu and choose **Customize my workspace**, then **Open source
project**. UX46 saves a recovery point and starts a local Codex conversation with
the installed source as its working folder. This gives that conversation project
access to the right files; it does not widen every conversation's permissions.
Its conversation records remain in private storage.

For another native agent, use `~/.local/bin/ux46 customize` in Terminal. It saves
the same recovery point and opens the configured CLI in the source folder when
run interactively. A source checkout without an installed-base manifest can still
be edited through your native agent's normal folder selection; save your own
backup first.

Here is a prompt you can adapt:

> My UX46 source is in `~/ux46`. I have trouble spotting the selected tab. Give it
> a clearer violet background while keeping the label easy to read. Find the
> relevant styles, save a local way to undo your changes, then make the smallest
> useful change. Show me what changed and how to check it. Keep my other edits.

That tells the AI where to work, what isn't working for you, and what a better
result looks like. You haven't had to guess a CSS selector or prescribe a new
frontend framework. A screenshot can help when “that tab” could mean several things.

## Look at the result, then adjust

Refresh your browser after a style change. Select several tabs. Can you identify
the current one without reading every label? Try your usual light or dark theme,
and make the window narrower. Keyboard focus should still be visible too.

If it isn't right, describe the difference you want:

> That's easier to see, but the label gets lost. Keep the fill and make the text
> clearer. Don't increase the tab height.

This feedback is the design work. You can be precise about the experience without
knowing the property names. Your AI should handle the implementation and explain
any tradeoff that affects you.

## Peek under the lid

The colors live in [the main stylesheet](../app/console/styles.css). Near the top,
look for names such as `--selection-bg` and `--selection-text`. These are **CSS
variables**: named values that several styles can use. A separate set of values
supports the light theme.

Names like `aria-selected="true"` describe interface state: this control is the
selected one. The rule using that state decides how it looks. Not every selected
control uses the same rule, so an AI should follow the actual tab's styles before
changing a shared color.

You can ask:

> Walk me through the lines you changed. Explain what each part controls, and
> what else would be affected if I changed this value again.

No exam follows. You now know one useful corner of the codebase.

## Keep a way back

To undo the source change, run these in Terminal. Stopping can interrupt active
owned work; let it finish first if needed.

```sh
~/.local/bin/ux46 stop
~/.local/bin/ux46 undo
~/.local/bin/ux46 open
```

Undo restores the latest saved source point. The previous source is preserved in
a separate backup folder, and private conversations and drafts are untouched.
The installed repair launcher lives outside editable source, so this works even
when a changed interface will not load. To save another point, use
`ux46 snapshot --label "Tabs I like"`; the command prints its ID for `ux46 undo --point ID`.

`ux46 source-status` compares your files with their installed base. Local changes
are flagged for review before any optional update; no automatic merge or updater
replaces them. Local Git history is also useful and remains optional. Git records
versions on your computer; GitHub is a separate place to publish them.

A **diff** shows the before and after. A **commit** saves a named version. Ask the
AI to show you its diff and explain the effect before you build on a change you
don't understand. Keep private state and conversation records out of code history.

## When the change gets bigger

“Make the tab clearer” mostly needs a visual check. “Change how conversations are
saved” needs checks that a conversation can be saved, found again, and kept separate
from someone else's work. [Tests](development.md) let the AI check those behaviors
with made-up data before you rely on them.

Ask for a small working slice of a big idea. Try it in real work, keep what helps,
and change what doesn't. A demo that looks right is a beginning; your daily use
will find the bits the demo never touched.

**Next: [Follow one click through the code →](code-tour.md)**
