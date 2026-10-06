from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from atlas_files import FileStore, FileStoreError, content_disposition, _open_regular_beneath  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\n" + b"tiny raster attachment"


class AtlasFileStoreTests(unittest.TestCase):
    def test_missing_attachment_does_not_close_a_reused_request_descriptor(self) -> None:
        # Model another server thread opening a socket immediately after the
        # failed attachment walk releases its root. It gets the same fd number.
        with tempfile.TemporaryDirectory() as temporary:
            original_close = os.close
            replacement = []
            def reuse_after_close(fd):
                original_close(fd)
                if not replacement:
                    replacement.append(os.open(os.devnull, os.O_RDONLY))
                    self.assertEqual(replacement[0], fd)
            try:
                with patch('atlas_files.os.close', side_effect=reuse_after_close):
                    with self.assertRaises(FileStoreError):
                        _open_regular_beneath(Path(temporary), Path('missing.png'))
                os.fstat(replacement[0])  # must still belong to the other request
            finally:
                if replacement:
                    try: original_close(replacement[0])
                    except OSError: pass

    def test_upload_persists_preview_metadata_and_download_across_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStore(root / "state")
            record = store.upload("alpha/room", "photo.png", PNG, "text/plain", project="alpha")

            self.assertEqual(record["room"], "alpha/room")
            self.assertEqual(record["name"], "photo.png")
            self.assertEqual(record["mime"], "image/png")
            self.assertEqual(record["sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertEqual(record["preview_url"], f"/api/atlas/files/{record['id']}/preview")
            self.assertEqual(record["download_url"], f"/api/atlas/files/{record['id']}/download")
            self.assertEqual(stat.S_IMODE((root / "state").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "state" / "files").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "state" / "atlas-files.sqlite3").stat().st_mode), 0o600)

            # Metadata and managed bytes remain available after a fresh store instance.
            reopened = FileStore(root / "state")
            loaded, downloaded = reopened.open_download(record["id"], room="alpha/room")
            self.assertEqual(loaded, {**record, "declared_mime": "image/png"})
            self.assertEqual(downloaded, PNG)
            with self.assertRaises(FileStoreError):
                reopened.get(record["id"], room="other/room")

    def test_native_copy_is_durable_and_denies_traversal_and_symlink_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            source = project / "generated.bin"
            payload = b"durable native attachment\x00"
            source.write_bytes(payload)
            external = root / "private.env"
            external.write_text("TOKEN=never-copy", encoding="utf-8")
            link = project / "escape"
            link.symlink_to(external)
            store = FileStore(root / "state")

            with self.assertRaises(FileStoreError):
                store.register_local(
                    "room", source, project_root=project, native_reference_validated=False
                )
            with self.assertRaises(FileStoreError):
                store.register_local(
                    "room", project / "../private.env", project_root=project,
                    native_reference_validated=True,
                )
            with self.assertRaises(FileStoreError):
                store.register_local(
                    "room", link, project_root=project, native_reference_validated=True
                )

            record = store.register_local(
                "room", source, project_root=project, native_reference_validated=True
            )
            source.unlink()
            loaded, downloaded = store.open_download(record["id"], room="room")
            self.assertEqual(downloaded, payload)
            self.assertEqual(loaded["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertIsNone(loaded["preview_url"])

    def test_download_type_is_allowlisted_and_the_declared_type_kept_for_display(self) -> None:
        # Downloads share the console origin under script-src 'self'; a stored
        # script, document or stylesheet type must never be served back as such.
        with tempfile.TemporaryDirectory() as temporary:
            store = FileStore(Path(temporary) / "state")
            cases = {
                "text/javascript; charset=utf-8": "application/octet-stream",
                "application/javascript": "application/octet-stream",
                "application/x-ecmascript": "application/octet-stream",
                "text/html": "application/octet-stream",
                "application/xhtml+xml": "application/octet-stream",
                "image/svg+xml": "application/octet-stream",
                "text/xml": "application/octet-stream",
                "text/css": "application/octet-stream",
                "video/vnd.xml-thing": "application/octet-stream",
                "made/up": "application/octet-stream",
                "text/plain": "text/plain",
                "application/pdf": "application/pdf",
                "audio/mpeg": "audio/mpeg",
                "video/mp4": "video/mp4",
            }
            for declared, served in cases.items():
                with self.subTest(declared):
                    record = store.upload("room", "notes.txt", b"alert(document.domain)", declared)
                    loaded, _ = store.open_download(record["id"], room="room")
                    self.assertEqual(loaded["mime"], served)
                    self.assertEqual(loaded["declared_mime"], record["mime"])
                    self.assertEqual(store.get(record["id"])["mime"], record["mime"])
            # Without a declared type, a guessed .js/.html/.svg type is narrowed too.
            for name in ("x.js", "x.html", "x.svg", "x.css"):
                record = store.upload("room", name, b"payload", None)
                self.assertEqual(store.open_download(record["id"])[0]["mime"],
                                 "application/octet-stream")

    def test_content_disposition_never_allows_filename_header_injection(self) -> None:
        header = content_disposition('report\r\nX-Evil: yes; "名".txt')
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertIn("filename*=UTF-8''", header)


class SkillCopyParityTests(unittest.TestCase):
    """The ux46-canvas skill ships copies of shared tools so it runs standalone.

    A drifted copy silently keeps fixed bugs (a double descriptor close, an
    unrestricted download type) alive in the skill. Each copy must match its
    tools/ source byte for byte, except upload.py's one import line, which
    names the skill-local ``room`` module instead of ``ux46_room``.
    """

    COPIES = {
        "atlas_desktops.py": ("atlas_desktops.py", {}),
        "atlas_files.py": ("atlas_files.py", {}),
        "board.py": ("ux46_boards.py", {}),
        "room.py": ("ux46_room.py", {}),
        "upload.py": ("ux46_upload.py",
                      {"from ux46_room import reference": "from room import reference"}),
    }

    def test_skill_copies_of_shared_modules_match_their_tools_sources(self) -> None:
        scripts = REPO_ROOT / "skills" / "ux46-canvas" / "scripts"
        for copy, (source, rewrites) in self.COPIES.items():
            with self.subTest(copy):
                expected = (REPO_ROOT / "tools" / source).read_text(encoding="utf-8")
                for original, replacement in rewrites.items():
                    self.assertIn(original, expected)
                    expected = expected.replace(original, replacement)
                self.assertEqual((scripts / copy).read_text(encoding="utf-8"), expected,
                                 f"skills copy {copy} drifted from tools/{source}")


if __name__ == "__main__":
    unittest.main()
