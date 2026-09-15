# Security and privacy

## Publication audit

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

## Installation and runtime boundaries

- Source and private state are separate. Installation refuses to replace an
  existing source directory, launcher or configuration. Nothing pushes local
  modifications, conversations or memories upstream.
- The installer fetches a versioned release through HTTPS and verifies a pinned
  SHA-256 before extracting it. It rejects unsafe archive paths and links.
  The checksum protects integrity relative to the installer; it is not an
  independent signature against compromise of the publishing account itself.
- If Python is missing, an explicitly versioned Astral uv installer can obtain a
  private Python runtime. Use `--no-python-download` to disallow this download.
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

Run `python3 scripts/check_public.py`, inspect the staged diff, and scan the full
history with a secret scanner. Do not post secrets in issues, CI output or
screenshots. If a real secret is discovered, revoke it with its provider and
remove the exposure; deleting the latest file alone does not clean Git history.
Report vulnerabilities privately through GitHub's security advisory facility
when available; do not publish working credentials or private data in an issue.
