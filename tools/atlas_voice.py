"""Optional local speech for the UX46 console.

Off unless the server is started with ``--voice-model-dir``. When on, it runs
Kokoro locally through the same process: no provider, no API key, no network
call. Nothing is spoken automatically, and only text a runtime already produced
is ever synthesized — the console never invents words to read aloud.

The model is loaded once, lazily, and every synthesis is serialized behind one
lock, because a single ONNX session is not safe to share concurrently.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

VOICES = ("af_heart", "am_michael")
DEFAULT_VOICE = "af_heart"

MAX_CHARS = 4000          # one request's worth of speech
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_CACHE_FILES = 400
KEY_RE = re.compile(r"^[0-9a-f]{32,64}$")


# -- what a reader should hear ---------------------------------------------
# Runtimes answer in Markdown. Kokoro reads what it is given, so the raw
# markers came out loud: "asterisk asterisk bold asterisk asterisk". This is a
# deliberately small, deterministic cleanup of *recognised* markers only. It is
# not a Markdown parser: anything it does not recognise is left alone, because
# an unread word is worse than a spoken stray character, and because an
# asterisk or a minus sign is often multiplication or a negative number rather
# than formatting.

SPEECH_VERSION = 1        # bump to retire cached audio made by older cleanup

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*\S*\s*$")
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")
_ORDERED_RE = re.compile(r"^\s*(\d{1,9})[.)]\s+")
_RULER_CELL_RE = re.compile(r"^:?-{2,}:?$")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>])")
_UNESCAPE_RE = re.compile("\x00(\\d+);")
_CODE_SPAN_RE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\((?:[^()\s]*)(?:\s+\"[^\"]*\")?\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((?:[^()\s]*)(?:\s+\"[^\"]*\")?\)")
_REF_LINK_RE = re.compile(r"\[([^\]\n]+)\]\[[^\]\n]*\]")
_AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
# The boundary guards are the point: "2*3*4" and "3 * 4" are arithmetic, and
# "well_known_name" is an identifier. None of those are emphasis.
_STRONG_STAR_RE = re.compile(r"(?<![A-Za-z0-9*])\*\*(?=\S)([^\n]+?)(?<=\S)\*\*(?![A-Za-z0-9*])")
_STRONG_BAR_RE = re.compile(r"(?<![A-Za-z0-9_])__(?=\S)([^\n]+?)(?<=\S)__(?![A-Za-z0-9_])")
_EM_STAR_RE = re.compile(r"(?<![A-Za-z0-9*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![A-Za-z0-9*])")
_EM_BAR_RE = re.compile(r"(?<![A-Za-z0-9_])_(?=\S)([^_\n]+?)(?<=\S)_(?![A-Za-z0-9_])")
_BLANKS_RE = re.compile(r"\n{3,}")


def speech_text(text: str) -> str:
    """Turn one runtime message into the words a voice should actually say.

    Markers the console recognises are dropped and their content kept. Bare
    text, bare URLs, arithmetic and escaped punctuation survive untouched.
    """

    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    fence = ""
    for raw in lines:
        fence_hit = _FENCE_RE.match(raw)
        if fence:
            # Inside a fence the code is read as written; only the ``` goes.
            if fence_hit and fence_hit.group(1)[0] == fence[0] and len(fence_hit.group(1)) >= len(fence):
                fence = ""
            else:
                out.append(raw.strip())
            continue
        if fence_hit:
            fence = fence_hit.group(1)
            continue
        line = raw
        while _QUOTE_RE.match(line):
            line = _QUOTE_RE.sub("", line, count=1)
        if _RULE_RE.match(line):
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            line = heading.group(1)
        else:
            ordered = _ORDERED_RE.match(line)
            if ordered:
                line = f"{ordered.group(1)}. " + line[ordered.end():]
            elif _BULLET_RE.match(line):
                line = _BULLET_RE.sub("", line, count=1)
        table = _table_row(line)
        if table is None:
            continue
        out.append(_inline(table).rstrip())

    joined = _BLANKS_RE.sub("\n\n", "\n".join(out))
    return joined.strip()


def _table_row(line: str) -> str | None:
    """A simple pipe row becomes a list of cells; a ruler row is silence."""

    stripped = line.strip()
    if not stripped.startswith("|") or "|" not in stripped[1:]:
        return line
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if cells and all(_RULER_CELL_RE.match(cell) for cell in cells if cell):
        return None
    return ", ".join(cell for cell in cells if cell)


def _inline(line: str) -> str:
    line = _ESCAPE_RE.sub(lambda m: "\x00%d;" % ord(m.group(1)), line)
    line = _CODE_SPAN_RE.sub(lambda m: m.group(2).strip(), line)
    line = _IMAGE_RE.sub(lambda m: m.group(1), line)
    line = _LINK_RE.sub(lambda m: m.group(1), line)
    line = _REF_LINK_RE.sub(lambda m: m.group(1), line)
    line = _AUTOLINK_RE.sub(lambda m: m.group(1), line)
    line = _STRIKE_RE.sub(lambda m: m.group(1), line)
    for pattern in (_STRONG_STAR_RE, _STRONG_BAR_RE, _EM_STAR_RE, _EM_BAR_RE):
        line = pattern.sub(lambda m: m.group(1), line)
    return _UNESCAPE_RE.sub(lambda m: chr(int(m.group(1))), line)


class VoiceError(RuntimeError):
    def __init__(self, message: str, code: str = "voice_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Speech:
    key: str
    path: Path
    voice: str
    sample_rate: int
    duration_s: float
    spoken_chars: int
    total_chars: int
    cached: bool
    synth_ms: int
    source_chars: int = 0     # Markdown as written, before the cleanup

    @property
    def complete(self) -> bool:
        return self.spoken_chars >= self.total_chars

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "voice": self.voice,
            "sample_rate": self.sample_rate,
            "duration_s": round(self.duration_s, 2),
            # Every char count below is of the spoken text, not the Markdown.
            "spoken_chars": self.spoken_chars,
            "total_chars": self.total_chars,
            "source_chars": self.source_chars,
            "speech_version": SPEECH_VERSION,
            # An explicit partial reading is honest; silent truncation is not.
            "complete": self.complete,
            "cached": self.cached,
            "synth_ms": self.synth_ms,
            "audio_url": f"/api/audio/{self.key}.wav",
        }


class VoiceService:
    """One lazily loaded local model behind one lock and a bounded cache."""

    def __init__(self, model_dir: str | Path, cache_dir: str | Path,
                 model_name: str = "kokoro-v1.0.onnx",
                 voices_name: str = "voices-v1.0.bin"):
        self.model_dir = Path(model_dir).expanduser()
        self.model_path = self.model_dir / model_name
        self.voices_path = self.model_dir / voices_name
        self.cache_dir = Path(cache_dir).expanduser()
        self._lock = threading.Lock()
        self._kokoro = None
        self._model_tag = ""
        self._load_ms = 0
        self._error = ""

    # -- availability ------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.model_dir)

    def status(self) -> dict:
        missing = [str(p) for p in (self.model_path, self.voices_path) if not p.is_file()]
        return {
            "enabled": not missing and not self._error,
            "loaded": self._kokoro is not None,
            "voices": list(VOICES),
            "default_voice": DEFAULT_VOICE,
            "max_chars": MAX_CHARS,
            "model_dir": str(self.model_dir),
            "missing": missing,
            "reason": self._error or ("model files not found" if missing else ""),
        }

    def _ensure_ready(self):
        if self._kokoro is not None:
            return self._kokoro
        for path in (self.model_path, self.voices_path):
            if not path.is_file():
                raise VoiceError(f"local voice model missing: {path}", code="voice_unavailable")
        try:
            from kokoro_onnx import Kokoro  # imported only when voice is on
        except Exception as exc:  # pragma: no cover - depends on the venv
            self._error = f"kokoro-onnx is not importable in this interpreter: {exc}"
            raise VoiceError(self._error, code="voice_unavailable") from exc
        started = time.time()
        try:
            self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))
        except Exception as exc:  # pragma: no cover - model load failure
            self._error = f"local voice model failed to load: {exc}"
            raise VoiceError(self._error, code="voice_unavailable") from exc
        self._load_ms = int((time.time() - started) * 1000)
        self._model_tag = _file_tag(self.model_path)
        return self._kokoro

    # -- synthesis ---------------------------------------------------------
    def key_for(self, text: str, voice: str) -> str:
        digest = hashlib.sha256()
        digest.update((self._model_tag or _file_tag(self.model_path)).encode())
        digest.update(b"\0")
        # Audio made before the Markdown cleanup said "asterisk"; retire it.
        digest.update(str(SPEECH_VERSION).encode())
        digest.update(b"\0")
        digest.update(voice.encode())
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        return digest.hexdigest()[:48]

    def path_for(self, key: str) -> Path:
        if not KEY_RE.match(key or ""):
            raise VoiceError("that is not an audio key", code="bad_key")
        return self.cache_dir / f"{key}.wav"

    def speak(self, text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0) -> Speech:
        if voice not in VOICES:
            raise VoiceError(f"unknown voice: {voice}", code="bad_voice")
        source = (text or "").strip()
        if not source:
            raise VoiceError("there is no text to read", code="empty_text")
        # Clean first: the length limit and the cache key are both about the
        # words that will actually be said, not the markers around them.
        prepared = speech_text(source)
        if not prepared:
            raise VoiceError("there is no text to read", code="empty_text")
        total = len(prepared)
        spoken = prepared[:MAX_CHARS]
        key = self.key_for(spoken, voice)
        path = self.path_for(key)
        if path.is_file():
            duration, rate = _wav_info(path)
            return Speech(key, path, voice, rate, duration, len(spoken), total,
                          True, 0, len(source))

        with self._lock:                       # one ONNX session, one job at a time
            if path.is_file():                 # another request just made it
                duration, rate = _wav_info(path)
                return Speech(key, path, voice, rate, duration, len(spoken), total,
                              True, 0, len(source))
            kokoro = self._ensure_ready()
            started = time.time()
            try:
                samples, sample_rate = kokoro.create(
                    spoken, voice=voice, speed=float(speed), lang="en-us"
                )
            except Exception as exc:
                raise VoiceError(f"local synthesis failed: {exc}", code="voice_failed") from exc
            synth_ms = int((time.time() - started) * 1000)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            _own_only(self.cache_dir, 0o700)
            _write_wav(path, samples, sample_rate)
            _own_only(path, 0o600)
            self._prune()
        duration, rate = _wav_info(path)
        return Speech(key, path, voice, rate, duration, len(spoken), total,
                      False, synth_ms, len(source))

    def _prune(self) -> None:
        try:
            files = sorted(self.cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        total = sum(p.stat().st_size for p in files)
        while files and (len(files) > MAX_CACHE_FILES or total > MAX_CACHE_BYTES):
            victim = files.pop(0)
            try:
                total -= victim.stat().st_size
                victim.unlink()
            except OSError:
                break


def _write_wav(path: Path, samples, sample_rate: int) -> None:
    import array

    scaled = array.array("h")
    for value in samples:
        clipped = 1.0 if value > 1.0 else (-1.0 if value < -1.0 else float(value))
        scaled.append(int(clipped * 32767))
    tmp = path.with_suffix(".part")
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(scaled.tobytes())
    os.replace(tmp, path)


def _wav_info(path: Path) -> tuple[float, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 1
            return frames / float(rate), rate
    except (OSError, wave.Error):
        return 0.0, 0


def _file_tag(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        return path.name


def _own_only(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


__all__ = ["VoiceService", "VoiceError", "Speech", "speech_text", "VOICES",
           "DEFAULT_VOICE", "MAX_CHARS", "SPEECH_VERSION"]
