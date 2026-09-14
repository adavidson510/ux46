from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from atlas_files import FileStore, FileStoreError, content_disposition  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\n" + b"tiny raster attachment"


class AtlasFileStoreTests(unittest.TestCase):
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
            self.assertEqual(loaded, record)
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

    def test_content_disposition_never_allows_filename_header_injection(self) -> None:
        header = content_disposition('report\r\nX-Evil: yes; "名".txt')
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertIn("filename*=UTF-8''", header)


if __name__ == "__main__":
    unittest.main()
