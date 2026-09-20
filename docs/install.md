[← UX46](../README.md) · **Install** · [Your first change →](first-change.md) · [AI path](ai-install.md)

# From this page to your first conversation

You need a Mac or Linux computer, an internet connection for installation, and a
coding agent to talk to. **Codex CLI** and **Claude Code** are the supported setup
choices. A CLI is a program you can run from a terminal; UX46 gives that program
a browser workspace. It doesn't include a model or a provider subscription.

If you already have an AI that can run commands on your computer, give it the
command below. It will be pointed to [its own setup guide](ai-install.md). You can
also do the install yourself. Both paths start here.

## 1 · Paste this

On a Mac, open **Terminal** from Spotlight. On Linux, open your terminal app.
A terminal is a window where you type instructions to your computer. Copy this
whole line, paste it there, and press Enter:

```sh
curl -fsSL https://raw.githubusercontent.com/adavidson510/ux46/main/install.sh | sh
```

`curl` downloads the installer. The `|` passes it to `sh`, which runs it. This
executes downloaded code on your computer, so it should come from a source you
trust. If you'd rather read it first, [open the installer](../install.sh), or
use the download-and-inspect option at the bottom of this guide.

Setup downloads a checked release into `~/ux46` and creates separate private
settings in `~/.ux46`. The `~` means your home folder. If a suitable Python is
missing, it can install a private Python 3.12 using
[Astral uv](https://docs.astral.sh/uv/guides/install-python/); it doesn't replace
system Python, ask for sudo, or edit your shell profile.

You don't need Git or a GitHub account. Installing UX46 doesn't publish your
work or sign you up for a shared service.

## 2 · Choose what you'll talk to

If setup finds only Codex or only Claude, it selects that CLI. If it finds both
or neither, it lets you choose. **Skip / connect my own later** is also valid:
that gets you a workspace, but conversations still need an agent connection.

If your chosen CLI is missing, setup finishes and points to the official
[Codex CLI](https://developers.openai.com/codex/cli) or
[Claude Code](https://code.claude.com/docs/en/setup) instructions. Install it and
sign in there, then start UX46. Your provider's charges and usage limits apply.
UX46 doesn't ask you to paste provider passwords or authentication files into it.

> **If your AI is doing the install:** captured output returns instructions instead
> of opening a server that would occupy its command tool. The AI should configure
> its actual runtime and start the workspace for you. A completed download alone
> doesn't prove the provider is signed in.

## 3 · Open a conversation

In a normal terminal, setup starts UX46 and opens your browser at a local address
such as `http://127.0.0.1:8877/`. “Local” means the app is served by your computer.
The address is not a public website other people can visit.

If the browser doesn't open, use the address printed in Terminal. If installation
returned without starting the app, run:

```sh
~/.local/bin/ux46 open
```

Click the **UX46 symbol** or **New conversation**. Start with something small and
useful, for example:

> Help me plan a small project. Ask what I want to make before creating files.

Name the conversation so you can find it again. Your agent's own permissions
still control its actions. You don't need to organize your old work before
starting something new here.

## 4 · Know how to leave and come back

You can close Terminal after `ux46 open` finishes. UX46 runs in the background
until you stop it or log out; no system-wide startup service is installed.
Closing a browser tab alone doesn't stop it.

```sh
~/.local/bin/ux46 stop
~/.local/bin/ux46 open
```

`stop` gracefully stops this installation and may interrupt its active owned
work. Conversations, drafts, settings and memories stay in their stores. `open`
reuses a running workspace or starts it and opens the browser. For foreground
diagnostics, `ux46 run` is still available; Control+C stops that foreground run.

## Where your things live

| Location | What's there | What you can do with it |
| :--- | :--- | :--- |
| `~/ux46` | Source: the interface, tools, docs, and skills | Let your AI edit your copy |
| `~/.ux46` | Private settings, registry, workspace stores and source recovery points | Back it up; keep it out of public contributions |
| `~/.ux46.install` | Resumable installer, optional private Python and independent repair launcher | Keep it with this installation; it is not a temporary download to delete |
| Your project's `sessions/` folder | Portable Session Vault records | Keep useful project context; treat it as private |
| Provider-managed storage | Native conversations and authentication | Leave ownership with Codex or Claude |

A **project** is a folder for a piece of work. The **registry** is UX46's list of
projects you've chosen to include. A **Session Vault record** saves useful context
and pointers; it isn't a second copy of the entire provider transcript.

To add a project, ask your AI to register the folder you choose. Or run this,
replacing the example path with the real folder on your computer:

```sh
~/.local/bin/ux46 project-add /path/to/my-project --name "My project"
```

The folder must already exist. Registration adds `project.json` if absent; future
records go in `sessions/`. Keep those private records out of public code uploads.
An existing nonempty `sessions/` directory is refused rather than imported without
review. The fresh install doesn't search through and reorganize your old chats.

## If something gets stuck

| What you see | What to try |
| :--- | :--- |
| “Existing path preserved” | The folder or launcher belongs to another installation. Use its existing launcher or choose separate locations; do not delete it to make setup proceed. |
| “Address already in use” | Another process may be using the port. Try `~/.local/bin/ux46 run --port 8878 --open`. |
| Browser opens, but the agent can't respond | Check that the selected CLI is installed and signed in using its official guide. `ux46 doctor --check` checks the selected command and reports available current evidence; unknown login or quota stays unknown. |
| `ux46: command not found` | Use the full `~/.local/bin/ux46` command. Your shell may not search that folder automatically. |
| Setup cannot download a file or finish configuration | Rerun the same command with the same locations and release. Its saved transaction resumes; completed files and your additions stay. If a crash happened before an ownership marker was saved, keep the error and inspect the unclaimed path; it will not be erased or guessed to be owned. |

For a small diagnostic report that doesn't start an agent:

```sh
~/.local/bin/ux46 doctor
```

Share the error and what you were trying to do when asking for help. Don't include
passwords, private conversation contents, or authentication files.

<details>
<summary><strong>More control: agent name, install options, and existing source</strong></summary>

To change the configured agent and its display name:

```sh
~/.local/bin/ux46 setup --agent claude --name "My helper"
```

Use `codex` instead for Codex. Stop and restart UX46 after configuration changes.
If you're simply changing a name, keep the same agent selection.

To install without starting a server:

```sh
curl -fsSL https://raw.githubusercontent.com/adavidson510/ux46/main/install.sh | sh -s -- --agent claude --no-start
```

Use `--agent codex` or `--agent none` instead if preferred. Captured output defaults
to no prompts and no server start; `--start` overrides that. Without a terminal,
ambiguous detection selects none; explicitly choosing `--agent auto` refuses to
pick when both CLIs are available.

Custom locations use `UX46_INSTALL_DIR`, `UX46_HOME`, and `UX46_BIN_DIR`. Set them
on the `sh` side of a curl pipeline, or download the script before running it.
Use separate absolute directories for source and private data. An existing
launcher is also protected; a second copy needs separate locations for all three.

Already downloaded or cloned the source? With Python 3.10+ and a terminal in that
source folder:

```sh
python3 tools/ux46 setup --agent auto
python3 tools/ux46 run --open
```

For another runtime, choose none and read [the adapter contract](adapter-contract.md).
A model endpoint alone isn't a session adapter. This is an extension path rather
than a guided install option for every model.

</details>

<details>
<summary><strong>Read before running, update your copy, or uninstall</strong></summary>

Download the installer to a new file, read it in an editor, then run
`sh install.sh` from that folder. It verifies a pinned source archive checksum
before extraction. [The security guide](../SECURITY.md) explains what that check
covers and what it doesn't.

There is no automatic updater. Before bringing in a newer release, back up your
copy and ask your AI to compare the changes with your local edits. A fresh install
in separate directories is another way to try a release without replacing yours.

To uninstall, stop UX46 and remove only its launcher and source directory after
checking their actual locations. Keep `~/.ux46` and project `sessions/` folders
if you want your settings and memories. The installer doesn't alter provider CLI
installations or authentication.

</details>

**Next: [Make one small change of your own →](first-change.md)**
