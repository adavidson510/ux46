#!/usr/bin/env python3
"""Optional Project Domain resolution for Atlas project manifests.

A Project Domain is the ownership, credential, storage, and policy boundary
that contains a project — ``demo-network``, ``demo-studio``, ``user``. It is
defined in ``architecture/PROJECT-DOMAINS-ARCHITECTURE-1.md``.

Three rules hold everywhere below.

*Additive.* The manifest field is optional. A manifest without ``domain``
remains valid and resolves to ``unscoped``. Absent metadata is never an
implicit grant of any domain, least of all the personal one.

*Canonical.* ``project.json`` owns the domain. Nothing else assigns it — not
keywords, not aliases, not ``related_projects``, not a session record, not a
central-store flag. Retrieval metadata is retrieval metadata.

*Fail closed on validity.* A missing field resolves to ``unscoped``; a present
but malformed field is an error, not a silent downgrade to ``unscoped``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


DOMAIN_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
UNSCOPED = "unscoped"
# Sentinels used by CLI filters and JSON output. A project may not claim one as
# its own domain id, or "--domain unscoped" would become ambiguous.
RESERVED_DOMAINS = frozenset({UNSCOPED, "none", "null", "any", "all"})
DOMAIN_NOTE = (
    "A domain is an ownership, credential, storage, and policy boundary. It is "
    "not visibility, not central-store classification, and not an access grant."
)


class DomainError(RuntimeError):
    """A user-facing project-domain error."""


def validate_domain(value: object, source: str) -> str:
    raw = value if isinstance(value, str) else ""
    if not isinstance(value, str):
        raise DomainError(f"Project domain must be a string in {source}: {value!r}")
    if raw != raw.strip():
        raise DomainError(
            f"Project domain may not have leading or trailing whitespace in {source}: {raw!r}"
        )
    if not raw:
        raise DomainError(
            f"Project domain cannot be empty in {source}. Remove the field to stay unscoped."
        )
    if raw in RESERVED_DOMAINS:
        raise DomainError(
            f"Project domain {raw!r} is reserved in {source}; "
            f"omit the field instead to report {UNSCOPED}"
        )
    if not DOMAIN_NAME.fullmatch(raw):
        raise DomainError(
            f"Invalid project domain in {source}: {raw!r} "
            "(expected lowercase a-z, 0-9, '.', '-', '_', up to 64 characters)"
        )
    return raw


def manifest_domain(manifest: object, source: str) -> str | None:
    """Read the canonical domain from one project manifest, and nothing else."""

    if not isinstance(manifest, dict):
        raise DomainError(f"Project manifest must be an object: {source}")
    if "domain" not in manifest or manifest["domain"] is None:
        return None
    return validate_domain(manifest["domain"], source)


def resolve_domain(project_root: Path) -> str | None:
    """Resolve a project's domain from its manifest, or ``None`` when unscoped."""

    manifest_path = Path(project_root) / "project.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainError(f"Invalid project manifest {manifest_path}: {exc}") from exc
    return manifest_domain(payload, str(manifest_path))


def domain_label(domain: str | None) -> str:
    return domain or UNSCOPED


def domain_filter(value: str) -> str:
    """Normalise a ``--domain`` filter, allowing the ``unscoped`` sentinel."""

    raw = str(value).strip()
    if raw == UNSCOPED:
        return UNSCOPED
    return validate_domain(raw, "--domain filter")


def matches_domain(domain: str | None, wanted: str | None) -> bool:
    if wanted is None:
        return True
    return domain_label(domain) == wanted
