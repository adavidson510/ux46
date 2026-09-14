"""Private, durable file storage for UX46 room attachments.

The browser only ever names an opaque file id.  This module retains a managed
copy before exposing metadata, so an attachment remains downloadable after the
original upload or generated asset is removed.  Authorization of the person is
owned by the console; ``room`` is accepted here to prevent accidental
cross-room attachment lookup.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_RASTER_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class FileStoreError(RuntimeError):
    """A controlled attachment-store refusal."""


def content_disposition(name: str) -> str:
    """Return an attachment header safe for ASCII and Unicode file names.

    The fallback is deliberately plain ASCII.  ``filename*`` carries the
    original printable Unicode name using RFC 5987 percent encoding.
    """

    safe_name = _safe_name(name)
    fallback = "".join(char if 32 <= ord(char) < 127 and char not in '"\\' else "_"
                       for char in safe_name)
    fallback = fallback.strip(" .") or "download"
    return "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
        fallback, quote(safe_name, safe="!#$&+-.^_`|~")
    )


class FileStore:
    """A local owner-only store of upload and console-validated native assets."""

    def __init__(self, state_dir: str | os.PathLike[str], *, max_upload_bytes: int = MAX_UPLOAD_BYTES):
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.max_upload_bytes = int(max_upload_bytes)
        self.files_dir = self.state_dir / "files"
        self.db_path = self.state_dir / "atlas-files.sqlite3"
        self._make_private_dir(self.state_dir)
        self._make_private_dir(self.files_dir)
        self._initialize()

    # -- public API -------------------------------------------------------

    def upload(
        self,
        room: str,
        name: str,
        data: bytes,
        mime: str | None = None,
        *,
        project: str | None = None,
    ) -> dict:
        """Persist explicit browser upload bytes and return its metadata record."""

        room = _required_label(room, "room")
        name = _safe_name(name)
        if not isinstance(data, bytes):
            raise FileStoreError("Upload data must be bytes")
        if len(data) > self.max_upload_bytes:
            raise FileStoreError(f"Upload exceeds the {self.max_upload_bytes} byte limit")
        digest = hashlib.sha256(data).hexdigest()
        self._write_blob_bytes(digest, data)
        return self._insert_record(room, name, digest, len(data), mime, project)

    def register_local(
        self,
        room: str,
        local_path: str | os.PathLike[str],
        *,
        project_root: str | os.PathLike[str],
        native_reference_validated: bool,
        generated_root: str | os.PathLike[str] | None = None,
        name: str | None = None,
        mime: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Copy a console-validated native attachment from an allowed local root.

        ``native_reference_validated`` is intentionally explicit: the console
        must first prove that this path came from the selected native item.  A
        caller cannot use this as a generic local-file uploader.
        """

        if native_reference_validated is not True:
            raise FileStoreError("Native file registration requires a console-validated reference")
        room = _required_label(room, "room")
        roots = [_canonical_root(project_root, "project root")]
        if generated_root is not None:
            roots.append(_canonical_root(generated_root, "managed generated root"))
        source = Path(local_path)
        if not source.is_absolute():
            raise FileStoreError("Native file reference must be an absolute path")
        chosen_root, relative = _select_root(source, roots)
        source_name = _safe_name(name if name is not None else relative.name)
        descriptor = _open_regular_beneath(chosen_root, relative)
        try:
            info = os.fstat(descriptor)
            if info.st_size > self.max_upload_bytes:
                raise FileStoreError(f"Attachment exceeds the {self.max_upload_bytes} byte limit")
            digest, size = self._write_blob_stream(descriptor)
        finally:
            os.close(descriptor)
        return self._insert_record(room, source_name, digest, size, mime, project)

    def get(self, file_id: str, *, room: str | None = None) -> dict:
        """Return metadata for one opaque id, optionally constrained to a room."""

        file_id = _file_id(file_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise FileStoreError("Attachment was not found")
        record = self._record(row)
        if room is not None and record["room"] != _required_label(room, "room"):
            raise FileStoreError("Attachment is not associated with this room")
        return record

    def open_download(self, file_id: str, *, room: str | None = None) -> tuple[dict, bytes]:
        """Return durable bytes only after resolving the opaque id and room."""

        record = self.get(file_id, room=room)
        blob = self._blob_path(record["sha256"])
        try:
            data = blob.read_bytes()
        except OSError as exc:
            raise FileStoreError("Stored attachment bytes are unavailable") from exc
        if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise FileStoreError("Stored attachment integrity check failed")
        return record, data

    def native_path(self, file_id: str, *, room: str | None = None) -> tuple[dict, Path]:
        """Resolve a managed path for the native adapter after room validation.

        The path is never derived from browser input.  It names the durable
        private copy, not the original source file.
        """

        record = self.get(file_id, room=room)
        path = self._blob_path(record["sha256"])
        try:
            info = path.stat()
        except OSError as exc:
            raise FileStoreError("Stored attachment bytes are unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size != record["size"]:
            raise FileStoreError("Stored attachment integrity check failed")
        return record, path

    def associate_project(self, file_id: str, project: str, *, room: str | None = None) -> dict:
        """Associate an existing managed raster upload with an authorized project icon."""

        record = self.get(file_id, room=room)
        if record["preview_url"] is None:
            raise FileStoreError("Only uploaded raster images may be used as project icons")
        project = _required_label(project, "project")
        with self._connect() as connection:
            connection.execute("UPDATE files SET project = ? WHERE id = ?", (project, file_id))
        return self.get(file_id, room=room)

    # -- persistence ------------------------------------------------------

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    room TEXT NOT NULL,
                    project TEXT,
                    name TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    preview_url TEXT,
                    download_url TEXT NOT NULL
                )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS files_room ON files(room)")
        try:
            os.chmod(self.db_path, 0o600)
        except OSError as exc:
            raise FileStoreError(f"Cannot secure attachment metadata: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.db_path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except sqlite3.Error as exc:
            raise FileStoreError(f"Cannot open attachment metadata: {exc}") from exc

    def _insert_record(
        self, room: str, name: str, digest: str, size: int, supplied_mime: str | None, project: str | None
    ) -> dict:
        mime = _mime_for(name, supplied_mime, self._blob_path(digest))
        file_id = secrets.token_urlsafe(24)
        preview_url = f"/api/atlas/files/{file_id}/preview" if _is_raster(self._blob_path(digest)) else None
        download_url = f"/api/atlas/files/{file_id}/download"
        project_value = _required_label(project, "project") if project is not None else None
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO files (id, room, project, name, mime, size, sha256, preview_url, download_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, room, project_value, name, mime, size, digest, preview_url, download_url),
            )
        return self.get(file_id, room=room)

    def _write_blob_bytes(self, digest: str, data: bytes) -> None:
        destination = self._blob_path(digest)
        self._make_private_dir(destination.parent)
        if destination.is_file():
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=destination.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _write_blob_stream(self, descriptor: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=".native-", dir=self.files_dir)
        try:
            os.fchmod(temporary_fd, 0o600)
            with os.fdopen(temporary_fd, "wb") as output, os.fdopen(os.dup(descriptor), "rb") as source:
                while True:
                    chunk = source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise FileStoreError(f"Attachment exceeds the {self.max_upload_bytes} byte limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            checksum = digest.hexdigest()
            destination = self._blob_path(checksum)
            self._make_private_dir(destination.parent)
            if not destination.is_file():
                try:
                    os.link(temporary_name, destination)
                except FileExistsError:
                    pass
            return checksum, size
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _blob_path(self, digest: str) -> Path:
        return self.files_dir / digest[:2] / digest

    @staticmethod
    def _make_private_dir(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise FileStoreError(f"Cannot secure attachment directory {path}: {exc}") from exc

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "room": row["room"], "name": row["name"], "mime": row["mime"],
            "size": row["size"], "sha256": row["sha256"], "preview_url": row["preview_url"],
            "download_url": row["download_url"], "project": row["project"],
        }


def _required_label(value: str, label: str) -> str:
    text = str(value).strip()
    if not text or "\x00" in text or len(text) > 255:
        raise FileStoreError(f"Invalid attachment {label}")
    return text


def _safe_name(value: str) -> str:
    text = str(value).replace("\r", "_").replace("\n", "_").replace("\x00", "_")
    text = Path(text.replace("\\", "/")).name.strip()
    if not text or text in {".", ".."}:
        return "download"
    return text[:255]


def _file_id(value: str) -> str:
    text = str(value)
    if not text or len(text) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in text):
        raise FileStoreError("Invalid attachment id")
    return text


def _canonical_root(value: str | os.PathLike[str], label: str) -> Path:
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise FileStoreError(f"Attachment {label} must be an existing absolute directory")
    return root.resolve()


def _select_root(source: Path, roots: list[Path]) -> tuple[Path, Path]:
    # Resolve first so aliases such as macOS /var -> /private agree with the
    # canonical root.  A link that escapes the root consequently fails the
    # containment test; the descriptor walk below still uses O_NOFOLLOW.
    lexical = source.resolve()
    for root in roots:
        try:
            relative = lexical.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            continue
        return root, relative
    raise FileStoreError("Native attachment is outside the selected project or managed generated root")


def _open_regular_beneath(root: Path, relative: Path) -> int:
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(root, flags | directory | nofollow)
    current = root_fd
    try:
        for index, part in enumerate(relative.parts):
            if part in {"", ".", ".."}:
                raise FileStoreError("Invalid native attachment path")
            final = index == len(relative.parts) - 1
            next_flags = flags | nofollow | (0 if final else directory)
            try:
                following = os.open(part, next_flags, dir_fd=current)
            except OSError as exc:
                raise FileStoreError("Native attachment is missing, symlinked, or inaccessible") from exc
            if current != root_fd:
                os.close(current)
            current = following
        info = os.fstat(current)
        if not stat.S_ISREG(info.st_mode):
            raise FileStoreError("Native attachment must be a regular file")
        result = current
        current = -1
        return result
    finally:
        if current >= 0:
            os.close(current)
        os.close(root_fd)


def _is_raster(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(32)
    except OSError:
        return False
    return _raster_mime(sample) is not None


def _raster_mime(sample: bytes) -> str | None:
    for signature, mime in _RASTER_SIGNATURES:
        if sample.startswith(signature):
            return mime
    if sample.startswith(b"RIFF") and len(sample) >= 12 and sample[8:12] == b"WEBP":
        return "image/webp"
    return None


def _mime_for(name: str, supplied: str | None, path: Path) -> str:
    try:
        with path.open("rb") as handle:
            detected = _raster_mime(handle.read(32))
    except OSError as exc:
        raise FileStoreError("Stored attachment bytes are unavailable") from exc
    if detected:
        return detected
    if supplied and "\r" not in supplied and "\n" not in supplied and "\x00" not in supplied:
        return supplied.split(";", 1)[0].strip().lower()[:127] or "application/octet-stream"
    guessed, _ = mimetypes.guess_type(name)
    # SVG and HTML are intentionally download-only even when their MIME is
    # known; their value here never grants a preview endpoint.
    return guessed or "application/octet-stream"
