"""Decide the execution permissions UX46 asks a native thread to run under.

Two policies, and the host chooses which one this console runs. They are not
the same kind of statement and are never confused for one another.

``preserve`` is the default and is a claim about the *session*: whatever the
CLI last recorded for this thread is what UX46 asks for, read from the exact
rollout path ``thread/read`` returned. Instruction and message prose is never
an authority source, and a permission representation this module cannot
reproduce exactly fails closed rather than being approximated — an unknown
restrictive policy is never claimed as preserved.

``full-access`` is a claim about the *host*, made once by whoever runs this
console for their own machine, and it deliberately overrides a saved
restrictive session profile: sandbox ``danger-full-access``, approval policy
``never``, approvals reviewed by the user rather than an auto-review subagent.
Nothing else changes. The exact thread id, working directory, workspace roots,
model, effort and the whole MCP and tool configuration are the ones already
there; no credential, account or configuration file is widened or touched.

Either way the request is only a request. Both paths read the runtime's own
effective values back and refuse if the host did not grant what was asked for,
so a session is never reported as running under a policy it is not running
under.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


class ProfileError(ValueError):
    pass


PRESERVE = "preserve"
FULL_ACCESS = "full-access"
POLICIES = (PRESERVE, FULL_ACCESS)

_MODES = {
    "danger-full-access": "dangerFullAccess",
    "workspace-write": "workspaceWrite",
    "read-only": "readOnly",
}
_BUILTINS = {
    "danger-full-access": ":danger-full-access",
    "workspace-write": ":workspace",
    "read-only": ":read-only",
}
_FIELDS = {
    "network_access": "networkAccess",
    "writable_roots": "writableRoots",
    "exclude_tmpdir_env_var": "excludeTmpdirEnvVar",
    "exclude_slash_tmp": "excludeSlashTmp",
}


def _absolute(value):
    return isinstance(value, str) and Path(value).is_absolute()


def _policy(value: dict, *, wire: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ProfileError("Native sandbox policy is missing")
    mode = value.get("type")
    if wire:
        mode = next((key for key, val in _MODES.items() if val == mode), None)
        value = {next((k for k, v in _FIELDS.items() if v == key), key): val
                 for key, val in value.items()}
    if mode not in _MODES:
        raise ProfileError("Native sandbox policy is unsupported")
    allowed = {"type"}
    if mode != "danger-full-access":
        allowed.add("network_access")
    if mode == "workspace-write":
        allowed.update(_FIELDS)
    if set(value) - allowed:
        raise ProfileError("Native sandbox policy contains unsupported restrictions")
    result = {"type": _MODES[mode]}
    for key in allowed - {"type"}:
        item = value.get(key, [] if key == "writable_roots" else False)
        if key == "writable_roots":
            if not isinstance(item, list) or not all(_absolute(p) for p in item):
                raise ProfileError("Native writable roots are invalid")
            item = sorted(set(item))
        elif not isinstance(item, bool):
            raise ProfileError("Native sandbox flag is invalid")
        result[_FIELDS[key]] = item
    return result


def _approval(value):
    if isinstance(value, str) and value in {"never", "on-request", "untrusted"}:
        return value
    if isinstance(value, dict) and set(value) == {"granular"}:
        flags = value["granular"]
        required = {"mcp_elicitations", "rules", "sandbox_approval"}
        optional = {"request_permissions", "skill_approval"}
        if (isinstance(flags, dict) and required <= set(flags) <= required | optional
                and all(isinstance(v, bool) for v in flags.values())):
            return {"granular": {k: flags.get(k, False) for k in sorted(required | optional)}}
    raise ProfileError("Native approval policy is missing or unsupported")


def _managed_workspace_equivalent(context: dict, sandbox: dict, cwd: str, roots: list) -> bool:
    """Recognize only the native built-in workspace policy's exact projection.

    The legacy sandbox omits protected metadata directories. Accept it only
    when the recorded managed policy contains exactly the generated rules;
    extra denies, missing protections, or conflicting projections must refuse.
    """
    def special(kind, access):
        return {"path": {"type": "special", "value": {"kind": kind}}, "access": access}

    def path_rule(path, access, **extra):
        return {"path": {"type": "path", "path": path}, "access": access, **extra}

    writable = sorted(set([cwd, *roots, *sandbox["writableRoots"]]))
    entries = [special("root", "read")]
    entries.extend(path_rule(path, "write") for path in writable)
    if not sandbox["excludeSlashTmp"]:
        entries.append(special("slash_tmp", "write"))
    if not sandbox["excludeTmpdirEnvVar"]:
        entries.append(special("tmpdir", "write"))
    for path in writable:
        for name in (".git", ".agents", ".codex"):
            entries.append(path_rule(str(Path(path) / name), "read", missing_path_behavior="skip"))

    # Rule order is not provenance; every exact rule, including its extra
    # fields and multiplicity, must still match the known generated set.
    def canonical(value):
        if not isinstance(value, list):
            return None
        return sorted(json.dumps(rule, sort_keys=True) for rule in value)

    permission = context.get("permission_profile")
    if not isinstance(permission, dict) or set(permission) != {"type", "file_system", "network"}:
        return False
    fs = permission["file_system"]
    if (permission["type"] != "managed"
            or permission["network"] != ("enabled" if sandbox["networkAccess"] else "restricted")
            or not isinstance(fs, dict) or set(fs) != {"type", "entries"}
            or fs["type"] != "restricted" or canonical(fs["entries"]) != canonical(entries)):
        return False
    projection = context.get("file_system_sandbox_policy")
    if projection is not None and (
        not isinstance(projection, dict) or set(projection) != {"kind", "entries"}
        or projection["kind"] != "restricted"
        or canonical(projection["entries"]) != canonical(entries)
    ):
        return False
    return context.get("active_permission_profile") in (
        None,
        {"id": ":workspace"}, {"id": ":workspace", "extends": None}
    )


@dataclass(frozen=True)
class ExecutionProfile:
    params: dict
    sandbox: dict
    source: dict
    policy: str = PRESERVE

    def as_json(self) -> dict:
        """What was asked for, and on whose authority. Never what was granted."""

        return {
            "policy": self.policy,
            "requested": {
                "sandbox": self.params["sandbox"],
                "approval_policy": self.params["approvalPolicy"],
                "approvals_reviewer": self.params["approvalsReviewer"],
            },
            "source": self.source,
        }

    def verify_start(self, response: dict) -> None:
        """A thread that did not exist a moment ago has no rollout to compare.

        Only the values the runtime just reported for it are checked, and only
        the ones this console asked for. A start response carries no workspace
        roots to check against, and an absent permission profile is not read as
        agreement with anything.
        """

        try:
            active = response.get("activePermissionProfile")
            matches = (
                response.get("cwd") == self.params["cwd"]
                and _policy(response.get("sandbox"), wire=True) == self.sandbox
                and _approval(response.get("approvalPolicy")) == self.params["approvalPolicy"]
                and response.get("approvalsReviewer") == self.params["approvalsReviewer"]
                and (active is None or (
                    isinstance(active, dict) and set(active) <= {"id", "extends"}
                    and active.get("id") == _BUILTINS[self.params["sandbox"]]
                    and active.get("extends") is None))
            )
        except (TypeError, ProfileError):
            matches = False
        if not matches:
            raise ProfileError(
                "This host asked the native runtime for full access and it did not "
                "grant it; UX46 has not taken control of the new session."
            )

    def verify(self, response: dict, thread_id: str) -> None:
        """Inspect effective native values, never merely trust requested overrides."""
        try:
            active = response.get("activePermissionProfile")
            active_ok = active is None or (
                isinstance(active, dict)
                and set(active) <= {"id", "extends"}
                and active.get("id") == _BUILTINS[self.params["sandbox"]]
                and active.get("extends") is None
            )
            matches = (
                (response.get("thread") or {}).get("id") == thread_id
                and response.get("cwd") == self.params["cwd"]
                and _policy(response.get("sandbox"), wire=True) == self.sandbox
                and _approval(response.get("approvalPolicy")) == self.params["approvalPolicy"]
                and response.get("approvalsReviewer") == self.params["approvalsReviewer"]
                and sorted(response.get("runtimeWorkspaceRoots", []))
                == sorted(self.params["runtimeWorkspaceRoots"])
                and "activePermissionProfile" in response and active_ok
            )
        except (TypeError, ProfileError):
            matches = False
        if not matches:
            raise ProfileError(
                "This host asked the native runtime for full access and it did not "
                "grant it; UX46 has not taken control of this session."
                if self.policy == FULL_ACCESS else
                "The native runtime did not preserve this session's execution profile; "
                "UX46 has not taken control. Continue in the CLI."
            )


def locate(thread: dict, thread_id: str) -> tuple[dict, dict]:
    """Identify the exact native thread and where it runs — nothing else.

    This answers "which thread, which directory, which workspace roots", and
    deliberately does not judge the permissions recorded on it. An explicitly
    chosen host policy replaces those permissions, so refusing here because
    the *old* saved policy is one this console could not have reproduced would
    refuse a session for a reason that no longer applies. Identity, an absolute
    working directory and absolute roots are still required, and a rollout that
    names a different thread is still refused.
    """

    context, source = _latest_turn_context(thread, thread_id)
    cwd = context.get("cwd")
    roots = context.get("workspace_roots", [cwd])
    if not _absolute(cwd) or not isinstance(roots, list) or not all(_absolute(p) for p in roots):
        raise ProfileError("Native working directory or workspace roots are invalid")
    return {"cwd": cwd, "runtimeWorkspaceRoots": roots}, source


def full_access(thread: dict, thread_id: str) -> ExecutionProfile:
    """What this host has said every session on it may do.

    The thread, its directory and its workspace roots come from the rollout;
    the permissions come from the host's own explicit choice and from nowhere
    else. No config override rides along, so the session's MCP servers, tools,
    model and effort are exactly the ones already configured.
    """

    where, source = locate(thread, thread_id)
    params = dict(where, sandbox="danger-full-access",
                  approvalPolicy="never", approvalsReviewer="user")
    return ExecutionProfile(
        params, {"type": "dangerFullAccess"},
        dict(source, policy=FULL_ACCESS, chosen_by="host"), FULL_ACCESS)


def full_access_start(cwd: str) -> ExecutionProfile:
    """The same host policy for a thread that does not exist yet."""

    if not _absolute(cwd):
        raise ProfileError("A new session needs an absolute working directory")
    params = {"cwd": cwd, "sandbox": "danger-full-access",
              "approvalPolicy": "never", "approvalsReviewer": "user"}
    return ExecutionProfile(
        params, {"type": "dangerFullAccess"},
        {"policy": FULL_ACCESS, "chosen_by": "host", "thread": "new"}, FULL_ACCESS)


def _latest_turn_context(thread: dict, thread_id: str) -> tuple[dict, dict]:
    """The newest recorded turn context on the exact rollout, read only."""

    path = thread.get("path")
    if thread.get("id") != thread_id or not _absolute(path):
        raise ProfileError("Native thread has no verifiable local rollout path")
    latest = None
    identified = False
    try:
        with Path(path).open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                record = json.loads(line)
                payload = record.get("payload") or {}
                if record.get("type") == "session_meta":
                    if payload.get("id") != thread_id:
                        raise ProfileError("Native rollout identity does not match the requested thread")
                    identified = True
                if record.get("type") == "turn_context":
                    latest = (payload, {
                        "path": path, "line": line_number,
                        "sha256": hashlib.sha256(line.encode()).hexdigest(),
                        "timestamp": record.get("timestamp"),
                        "turn_id": payload.get("turn_id"),
                    })
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        raise ProfileError("Native execution profile could not be read safely") from exc
    if not identified or latest is None:
        raise ProfileError("Native session has no structured execution profile to preserve")
    return latest


def from_thread(thread: dict, thread_id: str) -> ExecutionProfile:
    """Load latest actual turn context, with an exact source receipt.

    No cached UX46 default can replace a later CLI permission change. Repeated
    resumes and process restarts read the same native source of truth.
    """
    context, source = _latest_turn_context(thread, thread_id)
    sandbox = _policy(context.get("sandbox_policy"))
    mode = context["sandbox_policy"]["type"]
    cwd = context.get("cwd")
    roots = context.get("workspace_roots", [cwd])
    if not _absolute(cwd) or not isinstance(roots, list) or not all(_absolute(p) for p in roots):
        raise ProfileError("Native working directory or workspace roots are invalid")
    permission = context.get("permission_profile")
    # Managed rules are supported only when they exactly equal the known
    # native built-in projection; named or customized profiles still refuse.
    if permission is not None and not (
        (permission == {"type": "disabled"} and mode == "danger-full-access")
        or (mode == "workspace-write"
            and _managed_workspace_equivalent(context, sandbox, cwd, roots))
    ):
        raise ProfileError(
            "UX46 cannot yet reproduce this session's managed permission profile exactly; "
            "continue in the CLI"
        )
    active = context.get("active_permission_profile")
    if active is not None and active not in (
        {"id": _BUILTINS[mode]}, {"id": _BUILTINS[mode], "extends": None}
    ):
        raise ProfileError("UX46 cannot yet preserve this named permission profile")
    reviewer = context.get("approvals_reviewer", "user")
    if reviewer not in {"user", "auto_review", "guardian_subagent"}:
        raise ProfileError("Native approvals reviewer is unsupported")
    params = {"cwd": cwd, "sandbox": mode,
              "approvalPolicy": _approval(context.get("approval_policy")),
              "approvalsReviewer": reviewer, "runtimeWorkspaceRoots": roots}
    source = dict(source, policy=PRESERVE, chosen_by="session")
    if mode == "workspace-write":
        params["config"] = {"sandbox_workspace_write": {
            key: sandbox[field] for key, field in _FIELDS.items()
        }}
    elif mode == "read-only" and sandbox["networkAccess"]:
        # No verified config translation for network-enabled read-only yet.
        raise ProfileError("UX46 cannot yet preserve network-enabled read-only sessions")
    return ExecutionProfile(params, sandbox, source, PRESERVE)
