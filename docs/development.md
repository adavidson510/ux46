[← UX46](../README.md) · [Code tour](code-tour.md) · **Development** · [Contributing](../CONTRIBUTING.md)

# Check the thing you changed

A test is a repeatable example with an expected result. For setup, that might be
“running initialization twice preserves the existing settings.” It lets your AI
check a behavior again after changing the code, without asking you to rediscover
an old mistake.

Start with the part your change touches. A visual adjustment needs a browser
check; a change to saved data needs examples that prove existing data survives.
Use temporary folders and made-up conversations. Tests should never experiment
on your real sessions or quietly spend model turns.

## Running the checks

The commands below are for a terminal in the UX46 source folder. Your AI can run
them and explain failures. If you used the installer’s private Python, use the
Python executable in your private connection record rather than an older system
Python. For test dependencies, a temporary virtual environment keeps test tools
separate from your normal installation.

The core backend uses Python's standard library. No package installation is
needed to run it. Current Python sources require Python 3.10 or newer; Python
3.11+ is recommended. The frontend is plain JavaScript/CSS with local assets.

For a development checkout with Python 3.10+, create a **virtual environment**
(an isolated place for Python packages) and install the test tools:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
```

For a setup change, a focused check is:

```sh
python3 scripts/test.py test_setup.py
```

A passing test means its examples passed, not that every possible behavior was
proved. Inspect the diff and try the affected user flow too. Before contributing
or packaging a release, the full synthetic suite and publication guard are:

```sh
python3 scripts/test.py
python3 scripts/check_public.py
```

For conversation loading and connection state, run the browser journeys too:

```sh
npm ci --ignore-scripts
npx playwright install chromium
npm run test:ui
```

These need Node.js 22 or newer. They open the shipped interface in a disposable
Chromium browser with made-up conversations and delayed replies. No provider
login, real conversation, or model call is involved. The checks cover moving
between rooms and agents, catching up after a restart, and keeping unsent words.
Browser downloads and test dependencies stay outside the installed UX46 runtime.

The publication guard checks **staged files** in a Git checkout: the files
selected for the next commit. Review and stage your intended changes before
running it. In a downloaded source copy without Git history, it checks the source
tree. It looks for known private paths and credential patterns; it cannot prove
that arbitrary prose or screenshots contain no personal information.

## When you work on a runtime adapter

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

## Package a release · maintainers

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
