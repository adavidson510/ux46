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

Legacy native read tests require `UX46_TEST_REAL_RUNTIME=1` and an explicitly
chosen dedicated `UX46_TEST_THREAD`; leave both unset for CI. Never use real
user sessions for mutation tests.
Optional adapter integration tests require their documented runtime/toolchain.
The public entrypoint tests use a temporary UX46_HOME and fake native protocol
server; no paid model calls or provider credentials are needed.

Use `python3 tools/ux46 setup --agent claude` for standalone Claude.
Use `python3 tools/atlas_claude.py --help` for advanced separate adapter setup.
It needs its own state directory, the fresh registry path and an installed,
authenticated Claude CLI. Multi-agent connection settings go in the private
agents.json file, never this source tree. Native permissions default to
preserving the CLI's profile, not automatically granting full access.

Before a release, audit the exact staged files and run the standalone smoke
checks. Package tracked source only. Do not copy a development directory with
its local data. This source alpha has no built-in updater, installer daemon,
remote telemetry, or automatic contribution upload.

## Package a release

Stage only reviewed source, then run the publication guard and tests. Build with
`python3 scripts/package_release.py /absolute/path/outside/repo/ux46-source.tar.gz`.
The packager uses Git's index, fixes archive metadata, and excludes `install.sh`
to avoid a circular checksum. Running it twice against the same index produces
the same bytes. Never include a runtime directory or an existing user's copy.

Put the returned SHA-256 and the intended release tag in `install.sh`. Commit the
source and installer, publish the tagged source archive as a GitHub release asset,
and test the actual public download in fresh temporary source/data/bin locations
with `--agent none --no-start`. The archive is editable source without Git history.
Provider binaries are not bundled. Keep the installer and its referenced release
available together; a checksum mismatch must fail before extraction.
