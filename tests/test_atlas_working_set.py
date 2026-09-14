"""The working set: one or two relevant conversations per project.

A deterministic fixture stands in for the real machine: hundreds of records,
most of them pointing at threads the runtime itself recorded as sub-agent work,
several records aliasing one conversation, and five threads a person actually
started. Nothing here touches the real native catalogue, the real Vault or a
model — the native metadata adapter is pointed at a SQLite file this test
writes itself, read-only.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import atlas_console as console  # noqa: E402
import atlas_discovery as discovery  # noqa: E402
import atlas_native_meta as native_meta  # noqa: E402

FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_app_server.py"

DAY_MS = 86400 * 1000
BASE_MS = 1788000000 * 1000          # a fixed instant, so dates never drift

HUB = "01a00000-0000-7000-8000-00000000ff01"   # newest human thread, two records
HUB_SECOND = "01a00000-0000-7000-8000-00000000ff02"
OLDER = ["01a00000-0000-7000-8000-00000000ff0%d" % n for n in (3, 4, 5)]


def worker_thread(index: int) -> str:
    return "01a00000-0000-7000-8000-%012d" % index


def write_vault(base: Path) -> tuple[Path, Path]:
    """A registry, two projects, and a native catalogue that explains them."""

    hub_root = base / "Projects" / "hub"
    (hub_root / "sessions").mkdir(parents=True)
    other_root = base / "Projects" / "other"
    (other_root / "sessions").mkdir(parents=True)

    def record(root: Path, project: str, name: str, title: str, thread: str,
               updated: str, extra: str = "", aliases: str = "") -> None:
        sessions = root / "sessions"
        (sessions / f"{name}.md").write_text(
            "---\n"
            f"project: {project}\nsession: {name}\ntitle: {title}\n"
            f"date: {updated}\nupdated: \"{updated}\"\n"
            f"{'aliases: ' + aliases + chr(10) if aliases else ''}"
            f"keywords: fixture, {name}\norigins: \"{name}.origins.json\"\n"
            f"{extra}"
            "---\n\n# fixture\n\nSample body.\n",
            encoding="utf-8",
        )
        origins = []
        if thread:
            origins = [{"runtime": "codex", "node": "user-mac", "session_id": thread,
                        "cwd": "/tmp/atlas-fixture", "captured": updated, "primary": True}]
        (sessions / f"{name}.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": project, "session": name, "origins": origins,
        }), encoding="utf-8")

    status_active = "status: active\n"
    checkpoint = (
        "checkpoint_schema: 1\ncheckpoint_node: \"user-mac\"\n"
        "checkpoint_reporter: \"local-workspace\"\ncheckpoint_state: \"needs-human\"\n"
        "checkpoint_need: \"decision\"\ncheckpoint_next: \"Choose the label set.\"\n"
    )

    # One conversation, two records. The person named the ledger when they
    # started it; the second record is a later artifact of the same thread.
    record(hub_root, "hub", "continuity-ledger", "Hub continuity ledger", HUB,
           "2026-09-01", status_active, aliases="hub-ledger, legacy-ledger")
    record(hub_root, "hub", "now-facts", "Configurable now facts", HUB,
           "2026-09-01", status_active)
    record(hub_root, "hub", "staging-proof", "Staging proof", HUB_SECOND,
           "2026-09-01", status_active)
    for index, thread in enumerate(OLDER):
        record(hub_root, "hub", f"older-{index}", f"Older direct work {index}", thread,
               "2026-09-01", status_active)
    # No native origin at all: portable memory, and honestly unknown.
    record(hub_root, "hub", "portable-notes", "Portable notes", "", "2026-08-03",
           status_active)

    # Reported checkpoints that must not be presented as live obligations.
    record(hub_root, "hub", "finished-work", "Finished work", "",
           "2026-08-20", "status: complete\n" + checkpoint.replace(
               "checkpoint_schema: 1\n",
               "checkpoint_schema: 1\ncheckpoint_at: \"2026-08-20T10:00:00Z\"\n"))
    record(hub_root, "hub", "long-ago", "Long ago report", "", "2026-06-01",
           status_active + checkpoint.replace(
               "checkpoint_schema: 1\n",
               "checkpoint_schema: 1\ncheckpoint_at: \"2026-06-01T10:00:00Z\"\n"))
    record(hub_root, "hub", "worker-asked", "A worker asked", worker_thread(1),
           "2026-09-01", status_active + checkpoint.replace(
               "checkpoint_schema: 1\n",
               "checkpoint_schema: 1\ncheckpoint_at: \"2026-09-01T10:00:00Z\"\n"))

    # Hundreds of proven sub-agent conversations, some with several records.
    for index in range(1, 251):
        record(hub_root, "hub", f"agent-{index:03d}", f"Agent run {index}",
               worker_thread(index), "2026-09-02", status_active)
    for index in range(1, 21):
        record(hub_root, "hub", f"agent-{index:03d}-notes", f"Agent run {index} notes",
               worker_thread(index), "2026-09-02", status_active)

    record(other_root, "other", "only-room", "The only room here", "", "2026-07-04",
           status_active)

    registry = base / "registry.json"
    registry.write_text(json.dumps({
        "schema_version": 1, "updated": "2026-09-06", "node_id": "user-mac",
        "collective_id": "workspace",
        "nodes": [{"id": "user-mac", "name": "Fixture Mac", "status": "local"}],
        "projects": [
            {"id": "hub", "name": "Hub", "root": str(hub_root), "aliases": []},
            {"id": "other", "name": "Other", "root": str(other_root), "aliases": []},
        ],
    }), encoding="utf-8")

    native_db = base / "state_fixture.sqlite"
    build_native_db(native_db)
    return registry, native_db


def build_native_db(path: Path) -> None:
    """The shape the Codex CLI records, with the columns this adapter reads."""

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT NOT NULL,"
        " thread_source TEXT, updated_at INTEGER, updated_at_ms INTEGER,"
        " archived INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '',"
        " agent_role TEXT, agent_nickname TEXT, has_user_event INTEGER NOT NULL DEFAULT 0)"
    )
    rows = [
        # has_user_event stays 0 on real human threads; nothing may read it.
        (HUB, "exec", "user", BASE_MS,
         "Resume hub/continuity-ledger as the coordination hub"),
        (HUB_SECOND, "exec", "user", BASE_MS - DAY_MS, "Prove the staging path"),
        (OLDER[0], "cli", "user", BASE_MS - 8 * DAY_MS, "Older direct work 0"),
        (OLDER[1], "cli", "user", BASE_MS - 20 * DAY_MS, "Older direct work 1"),
        (OLDER[2], "cli", "user", BASE_MS - 30 * DAY_MS, "Older direct work 2"),
    ]
    for thread_id, source, kind, updated_ms, title in rows:
        conn.execute(
            "INSERT INTO threads(id, source, thread_source, updated_at, updated_at_ms,"
            " archived, title, has_user_event) VALUES(?,?,?,?,?,0,?,0)",
            (thread_id, source, kind, updated_ms // 1000, updated_ms, title),
        )
    spawn = json.dumps({"subagent": {"thread_spawn": {
        "parent_thread_id": HUB, "depth": 1, "agent_path": "/root/fixture"}}})
    for index in range(1, 251):
        conn.execute(
            "INSERT INTO threads(id, source, thread_source, updated_at, updated_at_ms,"
            " archived, title, has_user_event) VALUES(?,?,'subagent',?,?,0,?,0)",
            (worker_thread(index), spawn, (BASE_MS - DAY_MS) // 1000, BASE_MS - DAY_MS,
             f"agent run {index}"),
        )
    # A thread the catalogue knows nothing about stays unknown, not a worker.
    conn.commit()
    conn.close()


class Harness:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-working-set-"))
        registry, native_db = write_vault(self.tmp)
        self.native_db = native_db
        os.environ["ATLAS_FIXTURE_MODE"] = "normal"
        args = Namespace(
            host="127.0.0.1", port=0, state_dir=str(self.tmp / "state"),
            registry=str(registry), native_db=str(native_db),
            public_origin="", public_user="",
            codex_command=[sys.executable, str(FAKE_SERVER)],
            allow_test_thread=False, test_thread_cwd="",
            start_runtime=False, quiet=True,
        )
        self.service = console.ConsoleService(args)
        self.server = console.ConsoleServer(("127.0.0.1", 0), console.ConsoleHandler, self.service)
        self.port = self.server.server_address[1]
        self.service.auth.local_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.service.config.port = self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        try:
            self.service.workers.shutdown()
        finally:
            self.server.shutdown()
            self.server.server_close()
            shutil.rmtree(self.tmp, ignore_errors=True)
            os.environ.pop("ATLAS_FIXTURE_MODE", None)

    def call(self, method: str, path: str, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        head = {"Host": f"127.0.0.1:{self.port}"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            head["Content-Type"] = "application/json"
            head["Origin"] = f"http://127.0.0.1:{self.port}"
            head["X-Atlas-CSRF"] = self.service.csrf_token
        conn.request(method, path, body=payload, headers=head)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode("utf-8", "replace")[:200]}
        return response.status, parsed


class WorkingSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        for room in ("hub/continuity-ledger", "hub/now-facts", "hub/portable-notes",
                     "hub/agent-001", "hub/older-0"):
            self.h.service.journal.set_room_pref(room, pinned=False, hidden=False)
        self.h.service.discovery.refresh(force=True)

    def project(self, payload, project_id):
        return next(p for p in payload["projects"] if p["id"] == project_id)

    # -- the default view --------------------------------------------------
    def test_replacement_chapter_is_current_without_native_human_classification(self):
        parent = self.h.service.discovery.room("hub/continuity-ledger")
        chapter = replace(parent, session="chapter-two", thread_id=HUB_SECOND,
                          native_role=native_meta.UNKNOWN, native_names_me=False,
                          status="active", chapter_parent=parent.id, chapter_mode="replace")

        def project(rooms, prefs=None):
            return discovery._project_workspace(project_id="hub", name="Hub", root="/tmp",
                rooms=rooms, prefs=prefs or {}, suggest=1)

        result = project([parent, chapter])
        self.assertEqual(result["suggested"][0]["id"], chapter.id)
        self.assertEqual(result["suggested"][0]["reason"], "current_chapter")
        self.assertEqual(result["suggested"][0]["native_role"], native_meta.UNKNOWN)
        self.assertEqual(result["more_total"], 1)
        third = replace(chapter, session="chapter-three", thread_id=OLDER[0], chapter_parent=chapter.id)
        self.assertEqual(project([parent, chapter, third])["suggested"][0]["id"], third.id)
        for alternative in [replace(chapter, chapter_mode="parallel"),
                            replace(chapter, chapter_parent="other/missing"),
                            replace(chapter, status="complete"),
                            replace(chapter, native_archived=True),
                            replace(chapter, thread_id=parent.thread_id)]:
            self.assertNotEqual(project([parent, alternative])["suggested"][0]["reason"], "current_chapter")
        self.assertEqual(project([parent, chapter], {chapter.id: {"hidden": True}})["suggested"][0]["id"], parent.id)
        self.assertEqual(project([parent, chapter], {parent.id: {"pinned": True}})["pinned"][0]["id"], parent.id)

    def test_default_is_two_conversations_not_a_catalogue(self):
        status, payload = self.h.call("GET", "/api/workspace")
        self.assertEqual(status, 200)
        hub = self.project(payload, "hub")
        self.assertEqual(hub["record_total"], 280)
        self.assertEqual(len(hub["suggested"]), 2)
        self.assertEqual(hub["pinned"], [])
        self.assertEqual(
            [room["id"] for room in hub["suggested"]],
            ["hub/continuity-ledger", "hub/staging-proof"],
        )
        # The exact hub is chosen over its newer child record, and that record
        # is listed as the same conversation rather than as another chat.
        self.assertEqual(hub["suggested"][0]["aliases"], ["hub/now-facts"])
        self.assertEqual(hub["suggested"][0]["reason"], "recent")
        self.assertEqual(hub["suggested"][0]["group_role"], native_meta.HUMAN)

    def test_project_row_carries_no_alarming_totals(self):
        _status, payload = self.h.call("GET", "/api/workspace")
        hub = self.project(payload, "hub")
        self.assertNotIn("needs_person", hub)
        self.assertNotIn("controllable", hub)
        self.assertTrue(hub["last_active"])

    def test_proven_agent_work_is_excluded_by_default_and_counted_apart(self):
        _status, payload = self.h.call("GET", "/api/workspace")
        hub = self.project(payload, "hub")
        self.assertEqual(hub["agent_work_total"], 250)
        for room in hub["suggested"]:
            self.assertNotEqual(room["group_role"], native_meta.WORKER)

    def test_recency_comes_from_the_runtime_not_the_record_date(self):
        _status, payload = self.h.call("GET", "/api/workspace")
        hub = self.project(payload, "hub")
        first, second = hub["suggested"]
        self.assertEqual(first["recency_source"], "native")
        self.assertGreater(first["last_active"], second["last_active"])
        # Every record here claims 2026-09-01; only the runtime knows better.
        self.assertNotEqual(first["last_active"], first["updated"])

    def test_unknown_origin_is_neither_human_nor_worker(self):
        _status, payload = self.h.call("GET", "/api/rooms?project=hub&query=portable")
        room = next(r for r in payload["rooms"] if r["id"] == "hub/portable-notes")
        self.assertEqual(room["native_role"], native_meta.UNKNOWN)
        self.assertFalse(room["worker"])
        self.assertEqual(room["recency_source"], "record")

    def test_every_project_gets_the_same_treatment(self):
        _status, payload = self.h.call("GET", "/api/workspace")
        other = self.project(payload, "other")
        self.assertEqual([room["id"] for room in other["suggested"]], ["other/only-room"])

    # -- keeping and hiding ------------------------------------------------
    def test_pin_survives_the_suggestion_cap_and_round_trips(self):
        status, _ = self.h.call("POST", "/api/room/hub/older-0/pref", {"pinned": True})
        self.assertEqual(status, 200)
        _status, payload = self.h.call("GET", "/api/workspace")
        hub = self.project(payload, "hub")
        self.assertEqual([room["id"] for room in hub["pinned"]], ["hub/older-0"])
        self.assertEqual(hub["pinned"][0]["reason"], "pinned")
        self.assertEqual(len(hub["suggested"]), 2)
        self.assertNotIn("hub/older-0", [room["id"] for room in hub["suggested"]])

        self.h.call("POST", "/api/room/hub/older-0/pref", {"pinned": False})
        _status, payload = self.h.call("GET", "/api/workspace")
        self.assertEqual(self.project(payload, "hub")["pinned"], [])

    def test_hiding_removes_the_conversation_but_keeps_it_findable(self):
        self.h.call("POST", "/api/room/hub/continuity-ledger/pref", {"hidden": True})
        _status, payload = self.h.call("GET", "/api/workspace")
        hub = self.project(payload, "hub")
        self.assertEqual(hub["hidden_total"], 1)
        ids = [room["id"] for room in hub["suggested"]]
        # Neither the record that was hidden nor its sibling record comes back:
        # a person hid a conversation, not a filename.
        self.assertNotIn("hub/continuity-ledger", ids)
        self.assertNotIn("hub/now-facts", ids)
        self.assertEqual(ids[0], "hub/staging-proof")

        for query in ("continuity-ledger", "now-facts"):
            _status, found = self.h.call("GET", "/api/rooms?query=" + query)
            self.assertTrue([room for room in found["rooms"]
                             if query.replace("-", "") in room["id"].replace("-", "")],
                            f"{query} should still be findable")
        self.h.call("POST", "/api/room/hub/continuity-ledger/pref", {"hidden": False})

    def test_a_get_never_writes_a_preference(self):
        for path in ("/api/workspace", "/api/rooms?project=hub",
                     "/api/room/hub/older-0/pref"):
            self.h.call("GET", path)
        self.assertEqual(self.h.service.journal.room_prefs(), {})

    def test_a_preference_needs_a_real_room_and_a_boolean(self):
        status, payload = self.h.call("POST", "/api/room/hub/nope/pref", {"pinned": True})
        self.assertEqual(status, 404)
        status, payload = self.h.call("POST", "/api/room/hub/older-0/pref", {"pinned": "yes"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_pref")
        status, payload = self.h.call("POST", "/api/room/hub/older-0/pref", {})
        self.assertEqual(status, 400)

    # -- history and search ------------------------------------------------
    def test_history_is_paged_grouped_and_date_descending(self):
        _status, first = self.h.call("GET", "/api/rooms?project=hub&group=1&limit=5")
        self.assertTrue(first["grouped"])
        self.assertEqual(len(first["rooms"]), 5)
        self.assertTrue(first["truncated"])
        dates = [room["last_active"] for room in first["rooms"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        _status, second = self.h.call("GET", "/api/rooms?project=hub&group=1&limit=5&offset=5")
        self.assertFalse(set(r["id"] for r in first["rooms"])
                         & set(r["id"] for r in second["rooms"]))

    def test_agent_work_is_its_own_view_with_grouped_aliases(self):
        _status, payload = self.h.call(
            "GET", "/api/rooms?project=hub&workers=only&group=1&limit=200")
        self.assertEqual(payload["total"], 250)
        self.assertTrue(all(room["worker"] for room in payload["rooms"]))
        grouped = next(room for room in payload["rooms"] if room["id"] == "hub/agent-001")
        self.assertEqual(grouped["aliases"], ["hub/agent-001-notes", "hub/worker-asked"])

    def test_search_reaches_workers_old_records_and_aliases(self):
        _status, payload = self.h.call("GET", "/api/rooms?query=agent+run+7&workers=1")
        self.assertIn("hub/agent-007", [room["id"] for room in payload["rooms"]])
        _status, payload = self.h.call("GET", "/api/rooms?query=legacy-ledger")
        self.assertIn("hub/continuity-ledger", [room["id"] for room in payload["rooms"]])
        _status, payload = self.h.call("GET", "/api/rooms?query=" + HUB)
        self.assertIn("hub/continuity-ledger", [room["id"] for room in payload["rooms"]])

    def test_exact_id_lookup_answers_remembered_tabs(self):
        _status, payload = self.h.call(
            "GET", "/api/rooms?ids=hub/agent-001,hub/older-0,hub/nope")
        self.assertEqual([room["id"] for room in payload["rooms"]],
                         ["hub/agent-001", "hub/older-0"])

    # -- attention ---------------------------------------------------------
    def test_saved_reports_are_dated_context_not_live_badges(self):
        _status, payload = self.h.call("GET", "/api/attention")
        live = [room["id"] for room in payload["needs_person"]]
        suppressed = {room["id"]: room["suppressed_reason"]
                      for room in payload["reported_suppressed"]}
        self.assertNotIn("hub/finished-work", live)
        self.assertNotIn("hub/long-ago", live)
        self.assertNotIn("hub/worker-asked", live)
        self.assertIn("complete", suppressed["hub/finished-work"])
        self.assertIn("days ago", suppressed["hub/long-ago"])
        self.assertEqual(suppressed["hub/worker-asked"], "agent work")
        self.assertEqual(payload["needs_person_total"], 0)
        self.assertEqual(payload["approvals"], [])


class AliasPreferenceTests(unittest.TestCase):
    """A preference belongs to a conversation, not to one of its records."""

    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.service.journal.set_group_pref(
            ["hub/continuity-ledger", "hub/now-facts", "hub/staging-proof"],
            selected="hub/continuity-ledger", pinned=False, hidden=False)
        self.h.service.discovery.refresh(force=True)

    def hub(self):
        _status, payload = self.h.call("GET", "/api/workspace")
        return next(p for p in payload["projects"] if p["id"] == "hub")

    def test_hiding_one_alias_does_not_resurface_the_other(self):
        self.h.call("POST", "/api/room/hub/now-facts/pref", {"hidden": True})
        hub = self.hub()
        ids = [room["id"] for room in hub["suggested"] + hub["pinned"]]
        self.assertNotIn("hub/now-facts", ids)
        self.assertNotIn("hub/continuity-ledger", ids)
        self.assertEqual(hub["hidden_total"], 1)
        # Both records carry the same settled flag, written together.
        prefs = self.h.service.journal.room_prefs()
        self.assertTrue(prefs["hub/continuity-ledger"]["hidden"])
        self.assertTrue(prefs["hub/now-facts"]["hidden"])

    def test_two_pinned_aliases_render_one_conversation(self):
        for room in ("hub/continuity-ledger", "hub/now-facts"):
            self.h.call("POST", "/api/room/" + room + "/pref", {"pinned": True})
        hub = self.hub()
        self.assertEqual(len(hub["pinned"]), 1)
        row = hub["pinned"][0]
        self.assertEqual(row["aliases"], ["hub/continuity-ledger"])
        self.assertEqual(row["id"], "hub/now-facts")   # the record last chosen
        # Exactly one record claims the pin, so no second row can appear.
        pinned = [room for room, pref in self.h.service.journal.room_prefs().items()
                  if pref["pinned"]]
        self.assertEqual(pinned, ["hub/now-facts"])

    def test_a_legacy_contradiction_still_renders_one_canonical_row(self):
        # Two records pinned at once, as an older build could leave them.
        workspace = self.h.service.discovery.workspace({
            "hub/continuity-ledger": {"pinned": True},
            "hub/now-facts": {"pinned": True},
        })
        hub = next(p for p in workspace["projects"] if p["id"] == "hub")
        self.assertEqual(len(hub["pinned"]), 1)
        self.assertEqual(hub["pinned"][0]["id"], "hub/continuity-ledger")
        self.assertEqual(hub["pinned"][0]["aliases"], ["hub/now-facts"])

    def test_a_recent_pin_beats_an_older_hide_on_a_sibling(self):
        self.h.call("POST", "/api/room/hub/now-facts/pref", {"hidden": True})
        self.h.call("POST", "/api/room/hub/continuity-ledger/pref", {"pinned": True})
        hub = self.hub()
        self.assertEqual([room["id"] for room in hub["pinned"]], ["hub/continuity-ledger"])
        self.assertEqual(hub["hidden_total"], 0)

    def test_restoring_from_any_alias_settles_the_conversation(self):
        self.h.call("POST", "/api/room/hub/continuity-ledger/pref", {"hidden": True})
        status, payload = self.h.call("POST", "/api/room/hub/now-facts/pref", {"hidden": False})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(payload["conversation"]),
                         ["hub/continuity-ledger", "hub/now-facts"])
        self.assertEqual(self.h.service.journal.room_prefs(), {})
        hub = self.hub()
        self.assertEqual(hub["hidden_total"], 0)
        self.assertIn("hub/continuity-ledger", [room["id"] for room in hub["suggested"]])

    def test_unpinning_from_the_other_alias_clears_the_pin(self):
        self.h.call("POST", "/api/room/hub/continuity-ledger/pref", {"pinned": True})
        self.h.call("POST", "/api/room/hub/now-facts/pref", {"pinned": False})
        self.assertEqual(self.h.service.journal.room_prefs(), {})
        self.assertEqual(self.hub()["pinned"], [])

    def test_an_unrelated_conversation_keeps_its_own_preference(self):
        self.h.call("POST", "/api/room/hub/continuity-ledger/pref", {"hidden": True})
        prefs = self.h.service.journal.room_prefs()
        self.assertNotIn("hub/staging-proof", prefs)
        self.assertIn("hub/staging-proof", [room["id"] for room in self.hub()["suggested"]])


class ConversationIdentityTests(unittest.TestCase):
    """A native thread id is only unique inside its runtime and node."""

    def room(self, session, thread, *, project="hub", runtime="codex", node="user-mac"):
        return discovery.Room(
            project_id=project, project_name=project.title(), project_root="/tmp",
            session=session, title=session, status="active", updated="2026-09-01",
            capability="codex-local", thread_id=thread, cwd="", runtime=runtime,
            node=node, origin_count=1, origins_status="valid", checkpoint=None,
            attention={}, worker_provenance="", record_path="/tmp/x.md", keywords="",
        )

    def test_the_same_thread_id_on_another_node_is_another_conversation(self):
        here = self.room("here", HUB)
        there = self.room("there", HUB, node="server")
        other_runtime = self.room("other", HUB, runtime="claude-code")
        groups = discovery.group_rooms([here, there, other_runtime])
        self.assertEqual(len(groups), 3)
        self.assertEqual(len({room.conversation_key
                              for room in (here, there, other_runtime)}), 3)

    def test_the_same_thread_in_another_project_is_another_conversation(self):
        mine = self.room("here", HUB)
        theirs = self.room("here", HUB, project="other")
        self.assertEqual(len(discovery.group_rooms([mine, theirs])), 2)

    def test_the_same_thread_here_is_one_conversation(self):
        groups = discovery.group_rooms([self.room("a", HUB), self.room("b", HUB)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0][1]), 2)

    def test_a_record_without_a_native_session_is_only_itself(self):
        groups = discovery.group_rooms([self.room("a", ""), self.room("b", "")])
        self.assertEqual(len(groups), 2)

    def test_grouped_search_preserves_relevance_over_recent_topic_mentions(self):
        tell = self.room("boards", HUB, project="tell")
        alias = replace(self.room("tell-notes", HUB, project="tell"), updated="2026-09-02")
        mention = replace(self.room("ani4", HUB_SECOND), keywords="tell", updated="2026-09-09")
        catalog = discovery.Discovery()
        with patch.object(catalog, "rooms", return_value=[mention, alias, tell]):
            result = catalog.search("tell", group=True)
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["rooms"][0]["project_id"], "tell")
            self.assertEqual(result["rooms"][0]["alias_count"], 1)
            page = catalog.search("tell", group=True, limit=1, offset=1)
            self.assertEqual(page["rooms"][0]["id"], "hub/ani4")
            history = catalog.search(group=True)
            self.assertEqual(history["rooms"][0]["id"], "hub/ani4")

    def test_resume_suggestions_prefer_native_room_over_newer_portable_note(self):
        native = self.room("conversation", HUB)
        note = replace(self.room("note", ""), capability="vault-only", updated="2026-09-09")

        def workspace(prefs):
            return discovery._project_workspace(
                project_id="hub", name="Hub", root="/tmp", rooms=[note, native],
                prefs=prefs, suggest=1,
            )

        self.assertEqual(workspace({})["suggested"][0]["id"], native.id)
        pinned = workspace({note.id: {"pinned": True}})
        self.assertEqual(pinned["pinned"][0]["id"], note.id)
        self.assertEqual(pinned["suggested"][0]["id"], native.id)
        hidden = workspace({native.id: {"hidden": True}})
        self.assertEqual(hidden["suggested"][0]["id"], note.id)


class AdapterTests(unittest.TestCase):
    """The native catalogue is read, never written, and never assumed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-native-meta-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_catalogue_is_not_created_and_says_nothing(self):
        path = self.tmp / "absent.sqlite"
        adapter = native_meta.NativeMetadata(path)
        self.assertFalse(adapter.available)
        self.assertEqual(adapter.lookup([HUB]), {})
        self.assertFalse(path.exists())

    def test_missing_columns_degrade_to_less_provenance(self):
        path = self.tmp / "old.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT NOT NULL,"
                     " updated_at INTEGER, title TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO threads VALUES(?, 'cli', 1788000000, 'a title')", (HUB,))
        conn.commit()
        conn.close()
        adapter = native_meta.NativeMetadata(path)
        meta = adapter.lookup([HUB])[HUB]
        self.assertEqual(meta.role, native_meta.UNKNOWN)
        self.assertEqual(meta.updated_ms, 1788000000 * 1000)

    def test_a_spawned_thread_names_its_parent(self):
        path = self.tmp / "state.sqlite"
        build_native_db(path)
        adapter = native_meta.NativeMetadata(path)
        found = adapter.lookup([HUB, worker_thread(3), "not-a-thread"])
        self.assertEqual(found[HUB].role, native_meta.HUMAN)
        self.assertEqual(found[worker_thread(3)].role, native_meta.WORKER)
        self.assertEqual(found[worker_thread(3)].parent_thread_id, HUB)
        self.assertNotIn("not-a-thread", found)

    def test_a_native_title_is_a_hint_and_never_leaves_the_process(self):
        path = self.tmp / "state.sqlite"
        build_native_db(path)
        meta = native_meta.NativeMetadata(path).lookup([HUB])[HUB]
        self.assertTrue(meta.mentions("hub/continuity-ledger"))
        self.assertFalse(meta.mentions("hub/now-facts"))
        room = discovery.Room(
            project_id="hub", project_name="Hub", project_root="/tmp", session="x",
            title="T", status="active", updated="2026-09-01", capability="codex-local",
            thread_id=HUB, cwd="", runtime="codex", node="user-mac", origin_count=1,
            origins_status="valid", checkpoint=None, attention={}, worker_provenance="",
            record_path="/tmp/x.md", keywords="", native_names_me=True,
        )
        self.assertNotIn("coordination hub", json.dumps(room.as_json(full=True)))


if __name__ == "__main__":
    unittest.main()
