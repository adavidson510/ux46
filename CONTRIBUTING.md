# Contributing

You can customize and use UX46 without participating here. Contributions to
the shared base are optional.

1. Fork the public repository and create a branch for one change.
2. Develop with synthetic fixtures and private state outside the checkout.
3. Run the relevant tests and `python3 scripts/check_public.py`.
4. Open a pull request explaining the problem, resulting behavior, and checks.

A maintainer reviews the change, requests adjustments if needed, and merges
accepted contributions. A fork or pull request cannot change the official base
by itself. Never submit account files, local config, session transcripts,
private memory, access tokens, or screenshots containing personal work.

Keep new integrations optional. Default installations must work locally
without GitHub accounts, remote agents, mandatory sharing, or hosted services.
New dependencies need their license and installation requirements documented.
Do not redistribute vendor agent binaries or proprietary runtime assets.
