# UX46

**Your agents. Your work. Your space.**

A browser workspace around native coding agents: conversations, projects,
portable Session Vault records, and useful connections through Constellation.
Tell it what you need; shape your copy around how you work.

This is a standalone **alpha** for macOS and Linux. You get the complete editable
source and a fresh private workspace. There is no GitHub account or sharing requirement.

## Install it yourself, or give this to your AI

```sh
curl -fsSL https://raw.githubusercontent.com/adavidson510/ux46/main/install.sh | sh
```

The installer selects a detected Codex or Claude CLI, lets you choose when needed,
or lets you skip and connect another system later. Missing CLIs link to official
provider setup; login stays with the provider. Missing Python can be installed
privately. Nothing replaces an existing installation.

[Human install guide](docs/install.md) · [Security and privacy](SECURITY.md)

## Ask your AI to put it on

Paste the **same curl command above** into your AI. The installer points it to
the AI guide and, when run with captured output, returns control for setup
instead of opening a foreground server. No separate installation prompt is needed.

The AI path installs without starting a background job, configures its actual
runtime, and receives a small `connection.json` containing local tool paths and
memory entrypoints. It can then start the workspace and shape it around your work.
[AI setup guide](docs/ai-install.md) · [Other runtime adapters](docs/adapter-contract.md)

Prefer a ZIP or an existing checkout? With Python 3.10+:

```sh
python3 tools/ux46 setup --agent auto
python3 tools/ux46 run --open
```

## Make it yours

You have the complete source. Ask your AI to change the interface, workflow,
features, or code. There is no required upstream connection, sharing account,
or contribution step. Back up your working copy before substantial edits.
Git is useful local undo history, but optional.

Base updates are optional. There is no automatic updater overwriting your
changes. For a heavily customized copy, let your AI compare a new release with
your current source and bring across the changes you want. Automatic merging
of arbitrary modifications is not promised.

## What's included

| Component | Standalone default |
| --- | --- |
| Workspace and native Codex / Claude adapters | Choose at setup; no model turn during installation |
| Session Vault | Fresh local registry; project-owned Markdown and native pointers |
| Constellation | Empty local learning store; can be disabled |
| Tell / Signals | Optional; separate Tell service required |
| Email | Off; explicit account/OAuth configuration required |
| Scheduled and usage views | Local projections; collectors must be configured |
| Remote access and multiple machines | Optional; no hosted service supplied |

Change module flags in `~/.ux46/config.json` and restart. Disabling a module
preserves its data. No module is connected to anyone else's knowledge store.
See [architecture](docs/architecture.md) and [development](docs/development.md).

Native Windows installation, a signed desktop installer, automatic updates,
and remote multi-agent onboarding are not included in this alpha. Additional
runtime adapters are experimental source, not a promise of verified support.
Optional local voice needs separately obtained models and dependencies.
Vendor visualization kits and agent binaries are not redistributed.

## Contribute if you want

Use your own copy privately for as long as you like. To offer a reusable
improvement, fork the public repository, make a focused branch, and open a
pull request. Maintainers review and test before merging into the base.
See [CONTRIBUTING.md](CONTRIBUTING.md). No personal configuration, conversations,
credentials, or Constellation records belong in a contribution.

MIT licensed; see [LICENSE](LICENSE).
