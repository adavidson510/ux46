# UX46

**Your agents. Your work. Your space.**

A browser workspace around native coding agents: conversations, projects,
portable Session Vault records, and useful connections through Constellation.
Tell it what you need; shape your copy around how you work.

This is the first standalone **source alpha**. The workspace is substantial;
the friendly installer and first-run experience are still being built.

## Start locally

On macOS or Linux, install Python 3.10+ and your preferred native agent CLI.
The current single-command launcher uses an already installed and authenticated
Codex CLI. It does not install a provider, sign you in, or start a paid turn.

Download this repository as a ZIP and unpack it. No GitHub account or Git is
required. In the unpacked folder:

```sh
python3 tools/ux46 doctor
python3 tools/ux46 run
```

Open **http://127.0.0.1:8877**. Stop with Ctrl+C. If that port is occupied,
use `python3 tools/ux46 run --port 8878`.

The launcher creates a fresh private `~/.ux46` directory. Use `UX46_HOME` to
choose another location. Existing CLI authentication stays with the CLI.
Nothing imports your old project history or configures remote agents.
Use **New conversation** to begin an exploration. To register a project:

```sh
python3 tools/ux46 project-add /path/to/my-project --name "My project"
```

Registration adds a small `project.json` if absent; subsequent project sessions
use that project's `sessions/` directory. Registration refuses a nonempty old
`sessions/` directory rather than silently importing it. Keep those private
records out of any repository you publish.

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
| Workspace and native Codex adapter | On; runtime starts when needed |
| Session Vault | Fresh local registry; project-owned Markdown and native pointers |
| Constellation | Empty local learning store; can be disabled |
| Claude adapter | Included; separate adapter setup, not yet one-click onboarding |
| Tell / Signals | Optional; separate Tell service required |
| Email | Off; explicit account/OAuth configuration required |
| Scheduled and usage views | Local projections; collectors must be configured |
| Remote access and multiple machines | Optional; no hosted service supplied |

Change module flags in `~/.ux46/config.json` and restart. Disabling a module
preserves its data. No module is connected to anyone else's knowledge store.
See [architecture](docs/architecture.md) and [development](docs/development.md).

Native Windows installation, a signed desktop installer, automatic updates,
and turnkey multi-agent onboarding are not included in this alpha. Additional
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
