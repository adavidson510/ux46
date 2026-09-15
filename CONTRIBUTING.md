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

The [vibe-craft skill](skills/vibe-craft/SKILL.md) describes our approach to docs,
code commentary, and building with an AI. The [security guide](SECURITY.md) covers
private data and reporting a vulnerability. Neither requires you to speak like
a software company before we can understand your idea.
