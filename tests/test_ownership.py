"""Operational stewardship: two-party transfer, epochs, and immutable receipts.

Stewardship answers one question — which principal writes this record. These
tests drive the real `atlas own` CLI and assert the shape of the refusals as
carefully as the shape of the successes, because the refusals are the feature.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ATLAS = REPO_ROOT / "tools" / "atlas.py"
sys.path.insert(0, str(REPO_ROOT / "tools"))
import ownership  # noqa: E402

FAKE_TELL = r'''#!/usr/bin/env python3
"""A stand-in for the Tell client: records argv and returns a scripted receipt."""

import json
import os
import sys

log = os.environ.get("FAKE_TELL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\n")

if os.environ.get("FAKE_TELL_MODE") == "broken":
    print("tell: daemon unavailable", file=sys.stderr)
    sys.exit(4)

print(json.dumps({"envelope_id": "tel_env_test", "delivered": True}))
sys.exit(0)
'''


class OwnershipHarness:
    def build_world(self, base: Path) -> tuple[Path, Path]:
        project = base / "alpha"
        (project / "sessions").mkdir(parents=True)
        (project / "project.json").write_text(json.dumps({
            "schema_version": 1, "id": "alpha", "name": "Alpha", "status": "active",
            "visibility": "private", "aliases": [], "keywords": [], "sources": [],
        }), encoding="utf-8")
        for session in ("review", "builder"):
            (project / "sessions" / f"{session}.md").write_text(
                "---\n"
                f"project: alpha\nsession: {session}\ntitle: Alpha {session}\n"
                "date: 2026-08-20\nupdated: 2026-08-20\nstatus: active\n"
                f"keywords: {session}\n---\n\n"
                f"# alpha/{session}\n\n## Summary\n\nStewardship fixture.\n",
                encoding="utf-8",
            )
        registry = base / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1, "node_id": "user-mac",
            "projects": [{"id": "alpha", "name": "Alpha", "root": str(project), "aliases": []}],
        }), encoding="utf-8")
        return project, registry

    def own(
        self, registry: Path, *args: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("CODEX_THREAD_ID", None)
        if environment:
            env.update(environment)
        return subprocess.run(
            [sys.executable, str(ATLAS), "--registry", str(registry), "own", *args],
            capture_output=True, text=True, check=False, env=env,
        )

    def show(self, registry: Path, scope: str) -> dict:
        result = self.own(registry, "show", scope, "--json")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


class StewardshipTransferTests(OwnershipHarness, unittest.TestCase):
    def test_a_scope_is_unowned_until_someone_claims_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry = self.build_world(base)

            for scope in ("alpha", "alpha/review"):
                view = self.show(registry, scope)
                self.assertEqual(view["state"], "unowned")
                self.assertEqual(view["epoch"], 0)
                self.assertIsNone(view["owner"])
            # Reading never creates state.
            self.assertFalse((project / "ownership").exists())

            claimed = self.own(
                registry, "claim", "alpha/review", "--principal", "local-workspace", "--json"
            )
            self.assertEqual(claimed.returncode, 0, claimed.stderr)
            payload = json.loads(claimed.stdout)
            self.assertEqual(payload["ownership"]["owner"], "local-workspace")
            self.assertEqual(payload["ownership"]["epoch"], 1)
            self.assertEqual(payload["previous_owner"], None)
            self.assertIn("not legal or IP ownership", payload["authority"])
            self.assertTrue(Path(payload["receipt_path"]).is_file())
            # Project scope is a separate scope and is untouched.
            self.assertEqual(self.show(registry, "alpha")["state"], "unowned")

    def test_a_claim_never_displaces_a_steward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry = self.build_world(base)
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")

            second = self.own(
                registry, "claim", "alpha/review", "--principal", "cc-workspace"
            )
            self.assertEqual(second.returncode, 1)
            self.assertIn("already stewarded by 'local-workspace'", second.stderr)
            self.assertIn("distinct child room", second.stderr)
            self.assertEqual(self.show(registry, "alpha/review")["owner"], "local-workspace")

    def test_two_party_transfer_moves_the_writer_and_leaves_a_receipt_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry = self.build_world(base)
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")

            offered = self.own(
                registry, "offer", "alpha/review", "--principal", "local-workspace",
                "--to", "cc-workspace", "--if-epoch", "1",
                "--note", "handing over the central-store room", "--json",
            )
            self.assertEqual(offered.returncode, 0, offered.stderr)
            offer = json.loads(offered.stdout)["ownership"]
            self.assertEqual(offer["state"], "offered")
            self.assertEqual(offer["epoch"], 2)
            self.assertTrue(offer["offer_open"])
            # Offering does not move the writer.
            self.assertEqual(offer["owner"], "local-workspace")
            offer_id = offer["offer"]["offer_id"]

            accepted = self.own(
                registry, "accept", "alpha/review", "--principal", "cc-workspace",
                "--if-epoch", "2", "--if-offer", offer_id, "--json",
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            payload = json.loads(accepted.stdout)
            self.assertEqual(payload["ownership"]["owner"], "cc-workspace")
            self.assertEqual(payload["ownership"]["state"], "held")
            self.assertEqual(payload["ownership"]["epoch"], 3)
            self.assertIsNone(payload["ownership"]["offer"])
            self.assertEqual(payload["previous_owner"], "local-workspace")

            receipts = sorted(
                (project / "ownership" / "receipts" / "sessions" / "review").glob("*.json")
            )
            self.assertEqual(
                [path.name for path in receipts],
                ["000001-claim.json", "000002-offer.json", "000003-accept.json"],
            )
            chain = [json.loads(path.read_text(encoding="utf-8")) for path in receipts]
            for earlier, later in zip(chain, chain[1:]):
                self.assertEqual(later["previous_state_sha256"], earlier["state_sha256"])
            self.assertEqual(chain[-1]["event"], "accept")
            self.assertEqual(chain[-1]["from_owner"], "local-workspace")
            self.assertEqual(chain[-1]["to_owner"], "cc-workspace")
            self.assertEqual(chain[-1]["offer_id"], offer_id)

            # The receipt for a spent epoch is immutable; the offer is spent too.
            replay = self.own(
                registry, "accept", "alpha/review", "--principal", "cc-workspace"
            )
            self.assertEqual(replay.returncode, 1)
            self.assertIn("no open ownership offer", replay.stderr)

    def test_only_the_steward_offers_and_only_the_target_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry = self.build_world(base)
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")

            impostor = self.own(
                registry, "offer", "alpha/review", "--principal", "cc-workspace",
                "--to", "pane-workspace",
            )
            self.assertEqual(impostor.returncode, 1)
            self.assertIn("Only the current steward may offer", impostor.stderr)

            self_offer = self.own(
                registry, "offer", "alpha/review", "--principal", "local-workspace",
                "--to", "local-workspace",
            )
            self.assertEqual(self_offer.returncode, 1)
            self.assertIn("different principal", self_offer.stderr)

            self.own(
                registry, "offer", "alpha/review", "--principal", "local-workspace",
                "--to", "cc-workspace",
            )
            wrong_target = self.own(
                registry, "accept", "alpha/review", "--principal", "pane-workspace"
            )
            self.assertEqual(wrong_target.returncode, 1)
            self.assertIn("Only 'cc-workspace' may accept", wrong_target.stderr)
            self.assertEqual(self.show(registry, "alpha/review")["owner"], "local-workspace")

            # A second open offer is refused rather than silently replacing the first.
            again = self.own(
                registry, "offer", "alpha/review", "--principal", "local-workspace",
                "--to", "pane-workspace",
            )
            self.assertEqual(again.returncode, 1)
            self.assertIn("already has an open offer", again.stderr)

    def test_a_stale_compare_and_set_refuses_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry = self.build_world(base)
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")
            state_path = project / "ownership" / "sessions" / "review.json"
            before = state_path.read_bytes()

            stale = self.own(
                registry, "offer", "alpha/review", "--principal", "local-workspace",
                "--to", "cc-workspace", "--if-epoch", "7",
            )
            self.assertEqual(stale.returncode, 1)
            self.assertIn("Stale compare-and-set", stale.stderr)
            self.assertIn("Nothing was modified", stale.stderr)
            self.assertEqual(state_path.read_bytes(), before)

    def test_either_party_may_cancel_an_open_offer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry = self.build_world(base)
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")

            for actor, kind in (("local-workspace", "withdrawn"), ("cc-workspace", "declined")):
                with self.subTest(actor=actor):
                    self.own(
                        registry, "offer", "alpha/review", "--principal", "local-workspace",
                        "--to", "cc-workspace",
                    )
                    cancelled = self.own(
                        registry, "cancel", "alpha/review", "--principal", actor,
                        "--reason", "not proceeding", "--json",
                    )
                    self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
                    payload = json.loads(cancelled.stdout)
                    self.assertEqual(payload["receipt"]["cancel_kind"], kind)
                    # Cancelling never moves the writer.
                    self.assertEqual(payload["ownership"]["owner"], "local-workspace")
                    self.assertEqual(payload["ownership"]["state"], "held")

            stranger = self.own(
                registry, "offer", "alpha/review", "--principal", "local-workspace",
                "--to", "cc-workspace",
            )
            self.assertEqual(stranger.returncode, 0, stranger.stderr)
            refused = self.own(
                registry, "cancel", "alpha/review", "--principal", "pane-workspace"
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("may cancel this offer", refused.stderr)

    def test_the_record_refuses_secrets_multiline_notes_and_missing_rooms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry = self.build_world(base)

            secret = self.own(
                registry, "claim", "alpha/review", "--principal", "local-workspace",
                "--note", "api_key = " + "AKIA" + "0" * 16,
            )
            self.assertEqual(secret.returncode, 1)
            self.assertIn("secret-shaped content", secret.stderr)
            self.assertNotIn("AKIA" + "0" * 16, secret.stderr)

            multiline = self.own(
                registry, "claim", "alpha/review", "--principal", "local-workspace",
                "--note", "first line\nsecond line",
            )
            self.assertEqual(multiline.returncode, 1)
            self.assertIn("one printable line", multiline.stderr)

            missing = self.own(
                registry, "claim", "alpha/absent", "--principal", "local-workspace"
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("session-vault start alpha/absent", missing.stderr)

            traversal = self.own(registry, "show", "alpha/../../etc")
            self.assertEqual(traversal.returncode, 1)

    def test_an_expired_offer_cannot_be_accepted(self) -> None:
        scope = ownership.parse_scope("alpha/review")
        held, _ = ownership.apply_event(
            ownership.unowned_state(scope), scope,
            event="claim", actor="local-workspace", node="user-mac",
            at="2026-08-01T00:00:00.000000Z",
        )
        offered, _ = ownership.apply_event(
            held, scope, event="offer", actor="local-workspace", node="user-mac",
            at="2026-08-01T00:00:00.000000Z", target="cc-workspace", ttl=60,
        )
        with self.assertRaises(ownership.OwnershipError) as caught:
            ownership.apply_event(
                offered, scope, event="accept", actor="cc-workspace", node="server",
                at="2026-08-02T00:00:00.000000Z",
            )
        self.assertIn("expired", str(caught.exception))

        # Inside the lifetime the same accept succeeds and moves the writer.
        accepted, receipt = ownership.apply_event(
            offered, scope, event="accept", actor="cc-workspace", node="server",
            at="2026-08-01T00:00:30.000000Z",
        )
        self.assertEqual(accepted["owner"], "cc-workspace")
        self.assertEqual(receipt["epoch_after"], 3)

    def test_a_tell_notice_is_delivery_and_never_the_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry = self.build_world(base)
            tell_root = base / "telld"
            (tell_root / "src" / "tell").mkdir(parents=True)
            (tell_root / "src" / "tell" / "cli.py").write_text(FAKE_TELL, encoding="utf-8")
            log = base / "tell-argv.log"
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")

            offered = self.own(
                registry, "offer", "alpha/review", "--principal", "local-workspace",
                "--to", "cc-workspace", "--notify-tell", "--tell-root", str(tell_root), "--json",
                environment={"FAKE_TELL_LOG": str(log)},
            )
            self.assertEqual(offered.returncode, 0, offered.stderr)
            payload = json.loads(offered.stdout)
            self.assertTrue(payload["notify"]["sent"])
            self.assertEqual(payload["notify"]["to"], "cc-workspace")
            self.assertIn("never authorisation", payload["notify"]["authority"])
            argv = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(argv[0][:6], [
                "--as", "local-workspace", "send", "--to", "cc-workspace", "--intent",
            ])
            self.assertIn("notice", argv[0])
            # The notice did not transfer anything: cc must still accept.
            self.assertEqual(self.show(registry, "alpha/review")["owner"], "local-workspace")

            # A dead daemon degrades the notice; it never rolls back the act.
            cancelled = self.own(
                registry, "cancel", "alpha/review", "--principal", "local-workspace",
                "--notify-tell", "--tell-root", str(tell_root), "--json",
                environment={"FAKE_TELL_LOG": str(log), "FAKE_TELL_MODE": "broken"},
            )
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            payload = json.loads(cancelled.stdout)
            self.assertFalse(payload["notify"]["sent"])
            self.assertEqual(payload["ownership"]["state"], "held")
            self.assertIsNone(payload["ownership"]["offer"])


if __name__ == "__main__":
    unittest.main()
