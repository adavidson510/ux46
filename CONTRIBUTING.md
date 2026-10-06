[← UX46](README.md) · **Share a useful change** · [Development](docs/development.md)

# Made something useful?

You can keep it. You can also offer it back to the shared base so someone else
has a better starting point. Using UX46 never obliges you to contribute, and your
local copy doesn't need to look like ours.

A clearer error message counts. So does a guide that finally made something
click. You don't need to arrive with an impressive feature or pretend your AI
didn't help. Explain the problem, the result, and what you actually checked.

## If GitHub is new to you

GitHub hosts the public source and a place to discuss proposed changes. Reading
and installing don't need an account. Publishing a contribution here does.

A **fork** is your own GitHub copy of this repository. A **branch** holds a line
of work, such as one proposed fix. A **pull request** asks the maintainers to
review changes from your branch for inclusion in the shared base. Opening one
doesn't alter the official version or anyone else's local installation.

You can ask your AI:

> Help me contribute this change to UX46. Compare only the source I changed,
> explain what would be published, and keep my settings, conversations and memory
> private. Prepare a focused branch and a pull request describing the behavior
> and the checks we ran.

Give your AI access through the tools and account you choose. Don't paste a
password or access token into a conversation to make GitHub work.

## Prepare one useful change

1. Fork the public repository and create a branch for your change. Your AI can
   handle the Git commands and explain them as it goes.
2. Keep private state outside the checkout. Use made-up projects and conversations
   when testing; don't send test turns into your actual work.
3. Run [the focused checks](docs/development.md) for the behavior you changed and
   the publication guard. Review the diff too: an automated scan can't recognize
   every private detail.
4. Open a pull request with the problem, resulting behavior, and evidence. A
   maintainer may ask for adjustments, accept it, or explain why it doesn't fit
   the base. Your own copy remains yours either way.

For example, “Selected tabs were hard to spot in dark mode. This adds a visible
fill and keeps keyboard focus distinct. Checked both themes at desktop and phone
widths” tells a reviewer much more than “Improved UX.”

## What belongs in the shared base

Changes should keep the default install useful locally. Make integrations
optional; don't introduce required sharing, a GitHub login, or a hosted service.
Document new dependencies and their licenses. Provider binaries and proprietary
runtime assets must not be bundled.

Never submit local configuration, authentication files, session transcripts,
personal memories, or screenshots containing private work. A screenshot can show
more than the feature you meant to demonstrate. Use a fresh fixture workspace
for public examples.

## Check what you are making public

Review names and context as well as passwords. Private people, clients, projects,
device names, account IDs, home paths and email addresses can appear in fixtures,
comments, commit messages or image metadata. Prefer invented examples and use
"the human" in instructions for agents, and "you" in the interface. Deliberate
public branding and attribution can stay. Check images visually too.

For details specific to your installation, optionally create
`~/.ux46/private-markers.txt` **outside the checkout**, with one literal phrase
per line. Blank lines and lines beginning with `#` are ignored; matching is
case-insensitive. Use `chmod 600 ~/.ux46/private-markers.txt`. Choose distinctive
phrases: a common word can match unrelated code. The list is private and is never
uploaded by the check. `UX46_PRIVATE_MARKERS` or `--markers` can select another
owner-only file; an explicitly configured file that cannot be read fails the check.

`python3 scripts/check_public.py` checks the staged source, filenames and embedded
image text against that list and the built-in credential patterns. It does not
recognize every kind of personal information or read text from image pixels.
Check a proposed commit message with `--message-file PATH`, or messages already
committed with `--commits origin/main..HEAD`. The check reports the affected file,
not the private phrase. A filename containing a private phrase is also redacted.

Optional Git hooks check staged content before commit and the proposed message
before it is recorded. Run `git config --get core.hooksPath` first; if you already
use hooks, integrate these checks without replacing yours. Otherwise enable them:

```sh
git config core.hooksPath .githooks
```

Hooks use `python3`; `UX46_CHECK_PYTHON` can select your Python executable. Hooks
are local and can be bypassed. CI repeats the source and commit-message checks,
but **does not have your private marker list**. Review before pushing, not just
before merging. Deleting exposed content from main does not erase Git history,
old releases, forks or downloaded copies. Git author names and email addresses
are also public metadata; choose your preferred public identity or GitHub
no-reply address before committing. Do not invent another person's attribution.

The [vibe-craft skill](skills/vibe-craft/SKILL.md) describes our approach to docs,
code commentary, and building with an AI. The [security guide](SECURITY.md) covers
private data and reporting a vulnerability. Neither requires you to speak like
a software company before we can understand your idea.
