#!/usr/bin/env python3
"""A protocol fixture that speaks the Codex app-server wire format on stdio.

It exists so the console's rules (ownership, journaling, duplicate handling,
uncertain delivery, approvals, pagination) can be checked without spending a
model call or touching a real session. Shapes follow the generated schemas in
tools/atlas-live-v1-schema.

Behaviour switches through ATLAS_FIXTURE_MODE:
  normal    - answers everything promptly
  hang      - never answers turn/start (the console must report uncertain)
  approval  - asks for a command approval during the turn
  question  - asks a blocking user-input question during the turn
  unknown   - sends an unsupported server request
  reject    - rejects turn/start with a JSON-RPC error
  nolist    - answers thread/items/list with -32601, like the installed 0.153.4
              does for some threads, so the thread/read fallback is exercised
  empty     - a thread the runtime cannot read yet: both paths refuse
  nohistory - items/list refuses; thread/read succeeds with no turns at all,
              which is a genuinely new conversation
  badhistory- items/list refuses and thread/read fails with a real error, which
              is an unreadable history rather than an empty one
  authfail  - a saved terminal turn has the same managed-auth failure as the
              incident, so reload and aliases can recover it without a prompt
  active    - thread/list reports live work; refresh must refuse
  unknown_activity - thread/list cannot classify work; refresh must refuse
  loaded_paged_active - loaded-thread pagination finds active work on page two
  noturnslist - advertised turn paging is rejected for this stored thread
  needs_login - managed auth refresh completes but still needs sign-in
"""

from __future__ import annotations

import json
import os
import sys
import threading
import tempfile
from pathlib import Path
import time

MODE = os.environ.get("ATLAS_FIXTURE_MODE", "normal")
TURN_LOG = os.environ.get("ATLAS_FIXTURE_TURN_LOG", "")
THREAD_START_LOG = os.environ.get("ATLAS_FIXTURE_THREAD_START_LOG", "")
THREAD_ONE = "01a00000-0000-7000-8000-000000000001"
THREAD_TWO = "01a00000-0000-7000-8000-000000000002"

LOCK = threading.Lock()
ROLLOUTS = tempfile.TemporaryDirectory(prefix="atlas-fixture-rollouts-")


def emit(payload: dict) -> None:
    with LOCK:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def thread_meta(thread_id: str, name: str, cwd: str) -> dict:
    path = Path(ROLLOUTS.name) / (thread_id + ".jsonl")
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread_id}})
                    + "\n" + json.dumps({"type": "turn_context", "payload": {
                        "cwd": cwd, "workspace_roots": [cwd],
                        "approval_policy": "on-request", "approvals_reviewer": "user",
                        "sandbox_policy": {"type": "workspace-write"},
                    }}) + "\n")
    return {
        "path": str(path),
        "id": thread_id,
        "cwd": cwd,
        "name": name,
        "source": "cli",
        "model": "gpt-6-astra",
        "modelProvider": "openai",
        "reasoningEffort": "high",
        "createdAt": 1788600000,
        "updatedAt": 1788669188,
        "cliVersion": "0.153.4",
        "ephemeral": False,
        "status": ({"type": "active", "activeFlags": []}
                   if MODE in ("active", "loaded_paged_active")
                   else {"type": "systemError"} if MODE == "unknown_activity"
                   else {"type": "notLoaded"}),
        "turns": [],
    }


def item(turn_id: str, body: dict) -> dict:
    return {"turnId": turn_id, "item": body}


def history(thread_id: str) -> list[dict]:
    """A long-enough history that an early question falls outside one page."""

    turn_one = "01a00000-0000-7000-8000-0000000000t1"
    entries = [
        item(turn_one, {"type": "userMessage", "id": "i1", "clientId": None,
                        "content": [{"type": "text",
                                     "text": "Can I switch rooms without losing my place?"}]}),
        item(turn_one, {"type": "agentMessage", "id": "i2", "phase": "commentary",
                        "text": "Looking at the fixture tree now."}),
        item(turn_one, {"type": "commandExecution", "id": "i3", "command": "ls -1 fixture",
                        "cwd": "/tmp/atlas-fixture", "status": "completed", "exitCode": 0,
                        "durationMs": 42,
                        "aggregatedOutput": "alpha.txt\nbeta.txt\n"
                                            "a-very-long-fixture-line-" + "x" * 160 + "\n"}),
        item(turn_one, {"type": "agentMessage", "id": "i4", "phase": "final_answer",
                        "text": "Your place is kept per room, so switching is safe."}),
    ]
    # Enough intervening work that the early question is well outside the first
    # page the console loads.
    for n in range(5, 60):
        turn = f"01a00000-0000-7000-8000-0000000000t{n}"
        entries.append(item(turn, {
            "type": "commandExecution", "id": f"i{n}", "command": f"fixture-step {n}",
            "cwd": "/tmp/atlas-fixture", "status": "completed", "exitCode": 0,
            "durationMs": 10 + n, "aggregatedOutput": f"step {n} done\n"}))
    last = "01a00000-0000-7000-8000-0000000000t60"
    entries.extend([
        item(last, {"type": "userMessage", "id": "i60", "clientId": None,
                    "content": [{"type": "text", "text": "Add a third fixture file."}]}),
        item(last, {"type": "fileChange", "id": "i61", "status": "completed",
                    "changes": [{"path": "/tmp/atlas-fixture/gamma.txt", "kind": "add"}]}),
        item(last, {"type": "agentMessage", "id": "i62", "phase": "final_answer",
                    "text": "Added gamma.txt to the fixture directory."}),
    ])
    return entries


def turns_for(thread_id: str) -> list[dict]:
    """The same items, grouped the way thread/read reports them."""

    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for entry in history(thread_id):
        turn_id = entry["turnId"]
        if turn_id not in grouped:
            grouped[turn_id] = []
            order.append(turn_id)
        grouped[turn_id].append(entry["item"])
    turns = [{"id": turn_id, "status": "completed", "items": grouped[turn_id],
              "startedAt": 1788600000, "completedAt": 1788600100, "durationMs": 100}
             for turn_id in order]
    if MODE == "authfail":
        turns.append({
            "id": "01a00000-0000-7000-8000-0000000000af", "status": "failed", "items": [],
            "error": {"code": "unauthorized", "message":
                      "Your access token could not be refreshed because you have since logged out "
                      "or signed in to another account. Please sign in again."},
        })
    if MODE == "usagefail":
        turns.append({"id": "usage-turn", "status": "failed", "items": [],
                      "error": {"codexErrorInfo": "usageLimitExceeded", "message": "provider detail"}})
    return turns


def send_approval(thread_id: str, turn_id: str) -> None:
    time.sleep(0.15)
    emit({"jsonrpc": "2.0", "id": 9001, "method": "item/commandExecution/requestApproval",
          "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "i-approval",
                     "startedAtMs": int(time.time() * 1000),
                     "command": "rm -rf /tmp/atlas-fixture/scratch",
                     "cwd": "/tmp/atlas-fixture",
                     "reason": "the fixture asks before deleting"}})


def send_question(thread_id: str, turn_id: str) -> None:
    time.sleep(0.15)
    emit({"jsonrpc": "2.0", "id": 9002, "method": "item/tool/requestUserInput",
          "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "i-question",
                     "isBlocking": True,
                     "questions": [{"id": "q1", "title": "Which fixture branch should I use?",
                                    "options": ["alpha", "beta"]}]}})


def send_unknown(thread_id: str) -> None:
    time.sleep(0.1)
    emit({"jsonrpc": "2.0", "id": 9003, "method": "fixture/unsupportedRequest",
          "params": {"threadId": thread_id}})


def main() -> int:
    threads = {
        THREAD_ONE: thread_meta(THREAD_ONE, "Fixture console work", "/tmp/atlas-fixture"),
        THREAD_TWO: thread_meta(THREAD_TWO, "Fixture second room", "/tmp/atlas-fixture-2"),
    }
    loaded: set[str] = set()
    goals = {THREAD_ONE: {"threadId": THREAD_ONE, "objective": "Fixture goal",
                          "status": "usageLimited", "createdAt": 1, "updatedAt": 1,
                          "timeUsedSeconds": 2, "tokensUsed": 3, "tokenBudget": None}}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method", "")
        request_id = message.get("id")
        if request_id is None:  # notification or a response to our request
            continue

        def ok(result: dict) -> None:
            emit({"jsonrpc": "2.0", "id": request_id, "result": result})

        if method == "initialize":
            ok({"userAgent": "atlas-fixture/0.153.4", "codexHome": "/tmp/atlas-fixture-home",
                "platformFamily": "unix", "platformOs": "macos"})
        elif method == "thread/list":
            ok({"data": list(threads.values()), "nextCursor": None, "backwardsCursor": None})
        elif method == "thread/loaded/list":
            params = message.get("params") or {}
            if MODE == "loaded_paged_active":
                if not params.get("cursor"):
                    ok({"data": [THREAD_ONE], "nextCursor": "fixture-loaded-page-2"})
                elif params.get("cursor") == "fixture-loaded-page-2":
                    ok({"data": [THREAD_TWO], "nextCursor": None})
                else:
                    emit({"jsonrpc": "2.0", "id": request_id,
                          "error": {"code": -32602, "message": "bad fixture cursor"}})
            else:
                ids = sorted(loaded)
                if MODE in ("active", "unknown_activity"):
                    ids = [THREAD_ONE]
                ok({"data": ids, "nextCursor": None})
        elif method == "account/read":
            if MODE == "needs_login":
                ok({"requiresOpenaiAuth": True, "account": None})
            else:
                ok({"requiresOpenaiAuth": True,
                    "account": {"type": "chatgpt", "email": "fixture@example.test", "planType": "pro"}})
        elif method == "model/list":
            ok({"data": [
                {"id": "astra", "model": "gpt-6-astra", "displayName": "GPT-6 Astra",
                 "defaultReasoningEffort": "high", "supportedReasoningEfforts": [
                     {"reasoningEffort": "medium", "description": "fixture medium"},
                     {"reasoningEffort": "high", "description": "fixture high"},
                 ]},
                {"id": "terra", "model": "gpt-5.6-terra", "displayName": "GPT-5.6 Terra",
                 "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [
                     {"reasoningEffort": "low", "description": "fixture low"},
                     {"reasoningEffort": "medium", "description": "fixture medium"},
                 ]},
            ], "nextCursor": None})
        elif method == "thread/read":
            params = message.get("params") or {}
            tid = params.get("threadId", "")
            if tid not in threads:
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32602, "message": "unknown thread"}})
            elif MODE == "empty" and params.get("includeTurns"):
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32601, "message": "thread/read is not supported yet"}})
            elif MODE == "badhistory" and params.get("includeTurns"):
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32000,
                                "message": "rollout file is corrupt or unreadable"}})
            elif MODE == "nohistory" and params.get("includeTurns"):
                payload = dict(threads[tid])
                payload["turns"] = []
                ok({"thread": payload})
            else:
                payload = dict(threads[tid])
                if params.get("includeTurns"):
                    payload["turns"] = turns_for(tid)
                ok({"thread": payload})
        elif method == "thread/items/list":
            if MODE in ("nolist", "empty", "nohistory", "badhistory"):
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32601,
                                "message": "thread/items/list is not supported yet"}})
                continue
            params = message.get("params") or {}
            tid = params.get("threadId", "")
            limit = int(params.get("limit") or 40)
            cursor = params.get("cursor")
            data = history(tid)
            if params.get("sortDirection") == "desc":
                data = list(reversed(data))
            start = int(cursor) if cursor and str(cursor).isdigit() else 0
            page = data[start:start + limit]
            next_cursor = str(start + limit) if start + limit < len(data) else None
            ok({"data": page, "nextCursor": next_cursor,
                "backwardsCursor": str(start) if start else None})
        elif method == "thread/turns/list":
            if MODE == "noturnslist":
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32601, "message": "thread/turns/list is not supported yet"}})
                continue
            tid = (message.get("params") or {}).get("threadId", "")
            data = turns_for(tid)
            if (message.get("params") or {}).get("sortDirection") == "desc":
                data = list(reversed(data))
            limit = int((message.get("params") or {}).get("limit") or 40)
            ok({"data": data[:limit], "nextCursor": None, "backwardsCursor": None})
        elif method == "thread/resume":
            params = message.get("params") or {}
            if MODE == "metadata_resume" and not params.get("excludeTurns"):
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32602, "message": "full history hydration refused"}})
                continue
            tid = params.get("threadId", "")
            loaded.add(tid)
            thread = threads.get(tid) or thread_meta(tid, "Resumed fixture", "/tmp/atlas-fixture")
            ok({"thread": thread, "cwd": params.get("cwd", thread["cwd"]),
                "approvalPolicy": params.get("approvalPolicy", "on-request"),
                "approvalsReviewer": params.get("approvalsReviewer", "user"),
                "runtimeWorkspaceRoots": params.get("runtimeWorkspaceRoots", [thread["cwd"]]),
                "sandbox": {"type": "workspaceWrite", "writableRoots": [],
                            "networkAccess": False, "excludeSlashTmp": False,
                            "excludeTmpdirEnvVar": False},
                "activePermissionProfile": {"id": ":workspace"}})
        elif method == "thread/unsubscribe":
            loaded.discard((message.get("params") or {}).get("threadId", ""))
            ok({})
        elif method == "thread/start":
            params = message.get("params") or {}
            if THREAD_START_LOG:
                with open(THREAD_START_LOG, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(params) + "\n")
            new_id = "01a00000-0000-7000-8000-00000000000f"
            threads[new_id] = thread_meta(new_id, "Fixture test thread",
                                          params.get("cwd") or "/tmp/atlas-fixture")
            if params.get("model"):
                threads[new_id]["model"] = params["model"]
            ok({"thread": threads[new_id], "cwd": params.get("cwd") or "/tmp/atlas-fixture",
                "sandbox": params.get("sandbox"), "approvalPolicy": params.get("approvalPolicy"),
                "approvalsReviewer": "user", "model": "gpt-6-astra", "modelProvider": "openai"})
        elif method == "thread/settings/update":
            params = message.get("params") or {}
            thread = threads.get(params.get("threadId"))
            if thread is None:
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32602, "message": "unknown thread"}})
            else:
                if params.get("model") is not None:
                    thread["model"] = params["model"]
                if params.get("effort") is not None:
                    thread["reasoningEffort"] = params["effort"]
                ok({})
        elif method == "thread/compact/start":
            ok({})
        elif method == "thread/goal/get":
            ok({"goal": goals.get((message.get("params") or {}).get("threadId"))})
        elif method == "thread/goal/set":
            params = message.get("params") or {}
            goal = goals.get(params.get("threadId"))
            if goal is None:
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32602, "message": "unknown goal"}})
            else:
                if params.get("status") is not None:
                    goal["status"] = params["status"]
                goal["updatedAt"] += 1
                ok({"goal": goal})
        elif method == "thread/goal/clear":
            tid = (message.get("params") or {}).get("threadId")
            ok({"cleared": goals.pop(tid, None) is not None})
        elif method == "turn/start":
            params = message.get("params") or {}
            tid = params.get("threadId", "")
            if TURN_LOG:                      # one line per real dispatch
                with open(TURN_LOG, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"threadId": tid,
                                             "clientId": params.get("clientUserMessageId"),
                                             "input": params.get("input")}) + "\n")
            turn_id = "01a00000-0000-7000-8000-0000000000t9"
            if MODE == "hang":
                continue  # deliberately never answered
            if MODE == "reject":
                emit({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32000, "message": "fixture refused the turn"}})
                continue
            ok({"turn": {"id": turn_id, "status": "inProgress", "items": [],
                         "startedAt": int(time.time())}})
            emit({"jsonrpc": "2.0", "method": "turn/started",
                  "params": {"threadId": tid, "turn": {"id": turn_id, "status": "inProgress"}}})
            if MODE == "approval":
                threading.Thread(target=send_approval, args=(tid, turn_id), daemon=True).start()
            elif MODE == "question":
                threading.Thread(target=send_question, args=(tid, turn_id), daemon=True).start()
            elif MODE == "unknown":
                threading.Thread(target=send_unknown, args=(tid,), daemon=True).start()
            else:
                emit({"jsonrpc": "2.0", "method": "item/completed",
                      "params": {"threadId": tid, "turnId": turn_id,
                                 "item": {"type": "agentMessage", "id": "i-new",
                                          "phase": "final_answer",
                                          "text": "Fixture acknowledged the input."}}})
                emit({"jsonrpc": "2.0", "method": "turn/completed",
                      "params": {"threadId": tid,
                                 "turn": {"id": turn_id, "status": "completed"}}})
        elif method == "turn/steer":
            params = message.get("params") or {}
            if TURN_LOG:                      # steering is a dispatch too
                with open(TURN_LOG, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"threadId": params.get("threadId"),
                                             "clientId": params.get("clientUserMessageId"),
                                             "mode": "steer"}) + "\n")
            ok({"turn": {"id": params.get("expectedTurnId"), "status": "inProgress", "items": []}})
        elif method == "turn/interrupt":
            ok({})
        else:
            emit({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": f"fixture has no {method}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
