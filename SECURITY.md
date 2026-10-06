[← UX46](README.md) · [Install](docs/install.md) · **Security and privacy**

# Your source is shareable. Your work is private.

A fresh UX46 install creates a local workspace for you. It does not enroll you
in a shared memory service or upload your changes to this repository. You can
publish a code improvement without publishing the work you used it for.

Keep the distinction when customizing: source lives in `~/ux46`; settings and
local stores live in `~/.ux46`; portable session records live with your projects.
An AI asked to “upload my project” should check which files that includes.
Private settings and conversations should not come along for the ride.

## What the installer and app protect

The installer refuses to replace an existing copy or private settings. It checks
the downloaded source against a pinned checksum: a fingerprint of the expected
file. This catches a different or damaged archive before extraction. It is not
an independent signature if the publishing account itself is compromised.

The app listens on **loopback**, an address reached from your own computer. That
doesn't mean a native coding agent runs in a sandbox: its tools retain the
permissions you give the provider. Your model provider still receives the work
you send it according to its account settings and terms.

For the exact runtime boundaries and audit scope, keep reading.

<details>
<summary><strong>Public source audit · what was checked</strong></summary>

The initial public root commit `2b6e37d` was scanned with the repository's
publication guard and a separate private-marker check. A follow-up full-history
scan with Gitleaks 8.30.1 on 2026-09-14 found **zero secrets**. The scanner binary
was verified against its release checksum. These are scoped audit results,
not proof that every possible vulnerability or secret format has been excluded.

The public repository has fresh history. Private Git ancestry, authentication
files, configured email accounts, host addresses, session transcripts, personal
Constellation records, deployment receipts and runtime databases were not included.
Some tests contain deliberately synthetic credentials and identifiers to verify
redaction and rejection. They are not usable accounts.

</details>

## Installation and runtime boundaries

- Source and private state are separate. Installation refuses to replace an
  existing source directory, launcher or configuration. Nothing pushes local
  modifications, conversations or memories upstream.
- The installer fetches a versioned release through HTTPS and verifies a pinned
  SHA-256 before extracting it. It rejects unsafe archive paths and links.
  The checksum protects integrity relative to the installer; it is not an
  independent signature against compromise of the publishing account itself.
- The resumable installer folder (`~/.ux46.install`, or `$UX46_HOME.install`) must
  be a real folder owned by you that neither your group nor other users can write,
  and so must its saved `intent` and `python` files. Otherwise the installer stops
  before reading or running anything from it, so a pre-planted folder in a shared
  location cannot choose the program it runs.
- If Python is missing, an explicitly versioned Astral uv installer can obtain a
  private Python runtime. The uv installer script must match a pinned SHA-256
  before it runs, and it installs an exact CPython patch release that uv checks
  against its built-in checksums. uv runs from the installer's private temporary
  folder with configuration discovery off (`--no-config`, `UV_NO_CONFIG=1`), so
  a `uv.toml`, `pyproject.toml` or `.python-version` in the folder you ran the
  command from, or in your user or system uv settings, cannot choose the files.
  Variables that redirect uv or installer downloads, Python choice or install
  locations (for example `UV_DOWNLOAD_URL`, `UV_PYTHON_INSTALL_MIRROR`,
  `UV_PYTHON_DOWNLOADS_JSON_URL`, `UV_PYTHON`, `INSTALLER_DOWNLOAD_URL`) are
  cleared for those steps; proxy and certificate settings such as `HTTPS_PROXY`,
  `SSL_CERT_FILE` and `UV_NATIVE_TLS` are kept. The uv binary archive itself is
  fetched from GitHub over HTTPS by that script without a separate checksum.
  Use `--no-python-download` to disallow this download.
- Provider CLIs keep their own authentication. UX46 setup never reads auth files,
  asks for secrets or changes the account used by an installed CLI. Normal native
  runtime operations remain subject to that provider and the human's choices.
- The default server binds loopback. Changes require same-origin and a CSRF token.
  This is a local single-owner application, not a public multi-user service.
- Native tools can act with the permissions their owner gives them. Codex preserves
  native execution profiles; standalone Claude uses its default permission mode.
  UX46 setup does not grant bypass permissions or full disk access.
- Tell, email, shared learning and remote access are opt-in. The core does not
  provision an account, buy model usage, enable remote access or send telemetry.

Keep private project `sessions/` directories out of public repositories. Local
malware or another process running as your operating-system user is outside this
application's isolation boundary. Review your intended tools and project paths.

## Before contributing or releasing

Personal information needs a separate review from secrets. Inspect examples,
fixtures, image pixels/metadata, filenames, commit messages and author metadata
for details learned from a private installation. Public branding and intentional
attribution are different from private setup details. See CONTRIBUTING.md for
the optional local private-marker list and Git hooks. CI cannot check an owner's
private list, and these checks do not promise complete PII detection. Removing a
detail from the current source does not remove historical copies; avoid rewriting
shared history without a separately reviewed recovery plan.

Run `python3 scripts/check_public.py`, inspect the staged diff, and scan the full
history with a secret scanner. The guard refuses runtime and credential file names
(`.env` and `.env.*` other than examples, `.netrc`, `.npmrc`, SSH private key names,
`.p12`/`.pfx`, `connection.json`, `installation.json`) and common token formats
(private key blocks, GitHub, AWS, OpenAI-style, Stripe live, Slack, Google API,
Tailscale keys, JWTs and passwords in URLs); it is a guard, not a complete detector.
Tests that need sample tokens assemble them at runtime instead of storing them.
CI pins its GitHub Actions to full commit SHAs and does not keep the checkout token. Do not post secrets in issues, CI output or
screenshots. If a real secret is discovered, revoke it with its provider and
remove the exposure; deleting the latest file alone does not clean Git history.
Report vulnerabilities privately through GitHub's security advisory facility
when available; do not publish working credentials or private data in an issue.
