# Development

The core backend uses Python's standard library. No package installation is
needed to run it. Current Python sources require Python 3.10 or newer; Python
3.11+ is recommended. The frontend is plain JavaScript/CSS with local assets.

Install test-only dependencies with `python3 -m pip install -r requirements-dev.txt`.
Run only synthetic tests by default:

```sh
python3 scripts/test.py
python3 scripts/check_public.py
```

A legacy native read test additionally requires `UX46_TEST_REAL_RUNTIME=1`;
leave that unset for CI. Never use real user sessions for mutation tests.
Optional adapter integration tests require their documented runtime/toolchain.
The public entrypoint tests use a temporary UX46_HOME and fake native protocol
server; no paid model calls or provider credentials are needed.

Use `python3 tools/atlas_claude.py --help` for the separate Claude adapter.
It needs its own state directory, the fresh registry path and an installed,
authenticated Claude CLI. Multi-agent connection settings go in the private
agents.json file, never this source tree. Native permissions default to
preserving the CLI's profile, not automatically granting full access.

Before a release, audit the exact staged files and run the standalone smoke
checks. Package tracked source only. Do not copy a development directory with
its local data. This source alpha has no built-in updater, installer daemon,
remote telemetry, or automatic contribution upload.
