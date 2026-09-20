#!/usr/bin/env python3
"""UX46 access gate — a password in front of the private console.

The console already refuses anything that is not the configured private origin
carrying the exact identity its HTTPS front verified. That check is good and it
stays first here, unchanged: this process imports the console's own
``AuthBoundary`` rather than writing a second opinion about who is asking.

What it adds is one more thing to know: a password. It sits between the private
HTTPS front and the console, on loopback, and asks for it once — after which a
signed, identity-bound cookie stands in for it for a week, on every request:
the app, every API call, every attachment, the Tell surface. A tailnet identity
alone is no longer enough, and a password alone is no use without one.

    Tailscale Serve (TLS, verifies the identity)
        -> 127.0.0.1:8881   this gate      (identity, then password)
            -> 127.0.0.1:8877  the console
            -> 127.0.0.1:8879  Tell

The authenticated service recovery endpoint can gracefully restart the fixed
local console job; it cannot target another process or resend agent input.
All other paths forward what they are given. Set the password once:

    python3 tools/ux46_access_gateway.py --set-password \\
        --password-file ~/.local/state/ux46-access/password.json

It needs a Python whose ``hashlib`` has ``scrypt`` — that is, one built against
OpenSSL rather than Apple's LibreSSL. The interpreter the console already runs
under has it; the system ``python3`` on this Mac does not, and the gate says so
and refuses to start rather than silently hashing more weakly.

and then run it:

    python3 tools/ux46_access_gateway.py --port 8881 \\
        --public-origin https://workspace.example.com \\
        --public-user user@example.com \\
        --password-file ~/.local/state/ux46-access/password.json

See docs/ux46-access-gate-1.md.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from atlas_console import AuthBoundary  # noqa: E402
from atlas_voice import audio_response  # noqa: E402
from ux46_service_control import ServiceControl  # noqa: E402
import ux46_recovery as recovery  # noqa: E402

# ---------------------------------------------------------------------------
# fixed limits and shapes
# ---------------------------------------------------------------------------

FORMAT = "ux46-access-gate-password-1"
DEFAULT_PORT = 8881
def default_username() -> str:
    """Whoever is installing this, not a name that ships with the software.

    A shared default username is a shared guess. There is no default password
    at all: nothing works until an owner sets one.
    """
    try:
        return getpass.getuser() or "owner"
    except Exception:      # noqa: BLE001 - no controlling terminal, no passwd entry
        return "owner"
DEFAULT_UPSTREAM = "http://127.0.0.1:8877"
DEFAULT_TELL_UPSTREAM = "http://127.0.0.1:8879"
REALM = "UX46"

# The login this gate hands out once the password has been checked. `__Host-`
# is a promise the browser enforces: Secure, Path=/, and no Domain, so nothing
# on a neighbouring name can set or read it.
COOKIE_NAME = "__Host-ux46"
COOKIE_MAX_AGE = 7 * 24 * 60 * 60
COOKIE_VERSION = "1"
# Domain separation, so the key that signs a login can never be mistaken for
# the key that does anything else with the same secret.
COOKIE_KEY_LABEL = b"ux46-access-gate/cookie-signing/1"

MAX_UPLOAD = 64 * 1024 * 1024          # the console's own ceiling for one body
STREAM_CHUNK = 64 * 1024
UPSTREAM_TIMEOUT = 300.0               # a long-poll is a slow answer, not a hang

# scrypt at 16 MiB: a browser prompt should cost something, a page of API calls
# should not, which is what the cache below is for.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16


def scrypt_maxmem(n: int, r: int, p: int) -> int:
    """Room for the parameters actually in the file, not the ones compiled in."""
    return 128 * n * r * p * 2 + (1 << 20)


def scrypt_available() -> bool:
    """Apple's system Python is built against LibreSSL, which has no scrypt.

    Rather than quietly drop to a weaker derivation, this says so and stops.
    Run the gate with the same interpreter the console runs under.
    """
    return hasattr(hashlib, "scrypt")


# Nothing on this list may be copied through a proxy, in either direction.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})

# The only files this process will read out of the brand directory. Local owns
# these; the gate serves exactly these names and never a directory, a listing
# or anything reached by walking out of it.
BRAND_FILES = {
    "ux46-icon-1.svg": "image/svg+xml",
    "ux46-icon-32-1.png": "image/png",
    "ux46-apple-touch-icon-1.png": "image/png",
    "ux46-icon-192-1.png": "image/png",
    "ux46-icon-512-1.png": "image/png",
    "ux46-share-1.png": "image/png",
    "ux46-share-1.svg": "image/svg+xml",
    "ux46-manifest-1.webmanifest": "application/manifest+json",
}
BRAND_PREFIX = "/brand/"

# Fixed destinations. A path decides between two upstreams that were named on
# the command line; nothing a request carries can name a third.
TELL_PREFIX = "/api/tell"


# ---------------------------------------------------------------------------
# the password file
# ---------------------------------------------------------------------------

class PasswordFileError(Exception):
    """The stored password is missing, unreadable or not to be trusted."""


class StoredPassword:
    """Hash, salt and parameters. The password itself is never here."""

    def __init__(self, data: dict):
        self.username = str(data["username"])
        self.n = int(data["n"])
        self.r = int(data["r"])
        self.p = int(data["p"])
        self.dklen = int(data["dklen"])
        self.salt = base64.b64decode(data["salt"], validate=True)
        self.hash = base64.b64decode(data["hash"], validate=True)

    def derive(self, password: str) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=self.salt, n=self.n, r=self.r,
            p=self.p, dklen=self.dklen, maxmem=scrypt_maxmem(self.n, self.r, self.p),
        )

    def matches(self, password: str) -> bool:
        try:
            derived = self.derive(password)
        except (ValueError, MemoryError):
            return False
        return hmac.compare_digest(derived, self.hash)


def _own_only(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def load_password(path: Path) -> StoredPassword:
    """Read it, or refuse to start. There is no degraded mode."""
    if not path.exists() or path.is_symlink() or not path.is_file():
        raise PasswordFileError(f"no password file at {path}")
    info = path.stat()
    if info.st_uid != os.getuid():
        raise PasswordFileError(f"{path} is not owned by this account")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PasswordFileError(
            f"{path} is readable by other accounts; run chmod 600 on it")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PasswordFileError(f"{path} could not be read: {error}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise PasswordFileError(f"{path} is not a {FORMAT} file")
    if data.get("kdf") != "scrypt":
        raise PasswordFileError(f"{path} names a key derivation this gate cannot do")
    try:
        stored = StoredPassword(data)
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise PasswordFileError(f"{path} is missing or malforming a field: {error}") from None
    if not stored.username:
        raise PasswordFileError(f"{path} names no username")
    if len(stored.salt) < 16 or len(stored.hash) != stored.dklen or stored.dklen < 16:
        raise PasswordFileError(f"{path} has an implausible salt or hash length")
    if stored.n < 1 << 12 or stored.n & (stored.n - 1) or stored.r < 1 or stored.p < 1:
        raise PasswordFileError(f"{path} has parameters this gate will not accept")
    return stored


def write_password(path: Path, username: str, password: str) -> None:
    """Write the hash and nothing else, atomically, readable only by us."""
    if not (len(password) >= 8
            and any(c.isupper() for c in password)
            and any(c.islower() for c in password)
            and any(c.isdigit() for c in password)
            and any(not c.isalnum() and not c.isspace() for c in password)):
        raise PasswordFileError(
            "Use at least 8 characters, including uppercase, lowercase, "
            "a number and a symbol.")
    if not scrypt_available():
        raise PasswordFileError(
            "this Python has no hashlib.scrypt (it is built against LibreSSL). "
            "Use a Python built against OpenSSL 1.1 or later.")
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN, maxmem=scrypt_maxmem(SCRYPT_N, SCRYPT_R, SCRYPT_P),
    )
    payload = {
        "format": FORMAT,
        "username": username,
        "kdf": "scrypt",
        "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    _own_only(parent, 0o700)
    # Written beside the destination so the rename cannot cross a filesystem,
    # and created 0600 so it is never briefly world-readable.
    temporary = parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    _own_only(path, 0o600)


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class Login:
    """A signed note saying which identity answered the password, and until when.

    The key comes from the stored hash and salt, not from a secret of its own,
    which settles two things at once: restarting the gate keeps every login,
    because the file did not change; and setting a new password ends every
    login, because a new salt and hash mean a different key. There is no
    session table to keep, and nothing to leak if the process dies.
    """

    def __init__(self, stored: StoredPassword, max_age: int = COOKIE_MAX_AGE):
        self.key = hmac.digest(stored.hash + stored.salt, COOKIE_KEY_LABEL, "sha256")
        self.max_age = max_age

    def mint(self, identity: str, now: float = None, max_age: int = None) -> str:
        expires = int((time.time() if now is None else now)
                      + (self.max_age if max_age is None else max_age))
        payload = f"{COOKIE_VERSION}:{expires}:{identity.casefold()}".encode("utf-8")
        signature = hmac.digest(self.key, payload, "sha256")
        return _b64(payload) + "." + _b64(signature)

    def valid(self, token: str, identity: str) -> bool:
        """Signed by this key, not expired, and issued to exactly this person."""
        if not token or token.count(".") != 1:
            return False
        encoded, _, signature = token.partition(".")
        try:
            payload = _unb64(encoded)
            supplied = _unb64(signature)
        except (binascii.Error, ValueError):
            return False
        if not hmac.compare_digest(hmac.digest(self.key, payload, "sha256"), supplied):
            return False
        try:
            version, expires, issued_to = payload.decode("utf-8").split(":", 2)
        except (UnicodeDecodeError, ValueError):
            return False
        if version != COOKIE_VERSION:
            return False
        try:
            if int(expires) <= time.time():
                return False
        except ValueError:
            return False
        # The identity the HTTPS front just verified, not the one in the note:
        # a login belongs to one person and travels with nobody else.
        return hmac.compare_digest(issued_to, identity.casefold())

    def header(self, token: str) -> str:
        return (f"{COOKIE_NAME}={token}; Max-Age={COOKIE_MAX_AGE}; Path=/; "
                "Secure; HttpOnly; SameSite=Strict")


def read_cookie(raw: str, name: str) -> str:
    """One named cookie out of a header, without a parser that guesses."""
    for part in (raw or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value.strip()
    return ""


def cookies_without(raw: str, name: str) -> str:
    """Everything the browser sent except this gate's own login."""
    kept = []
    for part in (raw or "").split(";"):
        piece = part.strip()
        if not piece:
            continue
        key, sep, _ = piece.partition("=")
        if sep and key == name:
            continue
        kept.append(piece)
    return "; ".join(kept)


class Gate:
    """Everything the handler needs to decide, kept in one place and locked.

    The cache holds a keyed digest of an Authorization header that has already
    been checked, never the header and never the password. The key is minted at
    startup, so restarting the process — which is how a new password takes
    effect — throws every entry away.
    """

    CACHE_TTL = 300.0
    CACHE_MAX = 64
    FAIL_WINDOW = 60.0
    FAIL_LIMIT = 10
    FAIL_MAX_IDENTITIES = 512

    def __init__(self, stored: StoredPassword, username: str):
        self.stored = stored
        self.username = username
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._cache: "OrderedDict[bytes, float]" = OrderedDict()
        self._failures: "OrderedDict[str, list]" = OrderedDict()
        # Counted for the tests, so "the browser polling does not run scrypt
        # every second" is a claim something checks rather than a hope.
        self.kdf_calls = 0

    # -- the cache ---------------------------------------------------------
    def _token(self, identity: str, authorization: str) -> bytes:
        return hmac.digest(
            self._key,
            identity.casefold().encode("utf-8") + b"\x00" + authorization.encode("utf-8"),
            "sha256",
        )

    def _cached(self, token: bytes) -> bool:
        now = time.monotonic()
        with self._lock:
            expires = self._cache.get(token)
            if expires is None:
                return False
            if expires <= now:
                del self._cache[token]
                return False
            self._cache.move_to_end(token)
            return True

    def _remember(self, token: bytes) -> None:
        with self._lock:
            self._cache[token] = time.monotonic() + self.CACHE_TTL
            self._cache.move_to_end(token)
            while len(self._cache) > self.CACHE_MAX:
                self._cache.popitem(last=False)

    # -- rate limiting -----------------------------------------------------
    def throttled(self, identity: str) -> bool:
        """Too many wrong answers from one verified identity, lately."""
        now = time.monotonic()
        with self._lock:
            recent = [at for at in self._failures.get(identity, []) if at > now - self.FAIL_WINDOW]
            if recent:
                self._failures[identity] = recent
                self._failures.move_to_end(identity)
            else:
                self._failures.pop(identity, None)
            return len(recent) >= self.FAIL_LIMIT

    def _record_failure(self, identity: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = [at for at in self._failures.get(identity, []) if at > now - self.FAIL_WINDOW]
            recent.append(now)
            self._failures[identity] = recent[-self.FAIL_LIMIT:]
            self._failures.move_to_end(identity)
            while len(self._failures) > self.FAIL_MAX_IDENTITIES:
                self._failures.popitem(last=False)

    # -- the check ---------------------------------------------------------
    def check(self, identity: str, authorization: str) -> bool:
        if not authorization:
            return False
        token = self._token(identity, authorization)
        if self._cached(token):
            return True
        scheme, _, encoded = authorization.partition(" ")
        if scheme.strip().casefold() != "basic":
            self._record_failure(identity)
            return False
        try:
            raw = base64.b64decode(encoded.strip(), validate=True)
            user, sep, password = raw.decode("utf-8").partition(":")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            self._record_failure(identity)
            return False
        if not sep:
            self._record_failure(identity)
            return False
        # The username is compared in constant time too: it is not a secret,
        # but there is no reason for this to be the one branch that leaks.
        named = hmac.compare_digest(user.encode("utf-8"), self.username.encode("utf-8"))
        with self._lock:
            self.kdf_calls += 1
        correct = self.stored.matches(password)
        if not (named and correct):
            self._record_failure(identity)
            return False
        self._remember(token)
        return True


class Upstream:
    def __init__(self, origin: str):
        parsed = urlparse(origin)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError(f"{origin!r} must be an http:// origin on this machine")
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"{origin!r} must be a loopback address")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.origin = f"http://{parsed.netloc}"


class _Limited:
    """Exactly the bytes this request said it was sending, and no more.

    The socket carries the next request too; reading past Content-Length would
    take a bite out of it, and reading it all into memory would make a 64 MiB
    upload a 64 MiB allocation.
    """

    def __init__(self, source, length: int):
        self._source = source
        self._left = length

    def read(self, amount: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        want = self._left if amount is None or amount < 0 else min(amount, self._left)
        chunk = self._source.read(want)
        self._left -= len(chunk)
        return chunk


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ux46-access-gate/1"
    sys_version = ""

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):    # noqa: A003 - base class name
        """One line, and never a header. Credentials do not reach a log here."""
        if not self.server.gateway_verbose:
            return
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def log_request(self, code="-", size="-"):
        path = (self.path or "").split("?", 1)[0]
        self.log_message('"%s %s" %s', self.command, path, code)

    def _challenge(self) -> None:
        """Ask for the password, but only where a browser can actually ask.

        A page fetching in the background cannot show a prompt; answering it
        with `WWW-Authenticate` is how one expired login turns into a dialog
        every few seconds over a console nobody can reach. So the challenge
        goes to navigations and to callers that say nothing about their mode —
        curl, a script, a legacy client — and everything else gets a plain
        401 it can handle itself.
        """
        mode = self.headers.get("Sec-Fetch-Mode")
        if mode is None or mode.strip().casefold() == "navigate":
            self._refuse(
                HTTPStatus.UNAUTHORIZED, "a password is needed for UX46",
                {"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'})
            return
        body = json.dumps({"error": "workspace_login_required",
                           "message": "this UX46 login has expired; open the page again"})
        self._refuse(HTTPStatus.UNAUTHORIZED, body,
                     content_type="application/json")

    def _emit_login(self) -> None:
        """Set the cookie on whatever answer this request ends up with."""
        issue = getattr(self, "_issue", "")
        if issue:
            self.send_header("Set-Cookie", issue)
            self._issue = ""

    def _refuse(self, status: HTTPStatus, message: str, extra: dict = None,
                content_type: str = "text/plain; charset=utf-8") -> None:
        body = (message + "\n").encode("utf-8")
        self.send_response(status)
        self._emit_login()
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- the one path every request takes ---------------------------------
    def handle_one(self) -> None:
        server = self.server
        self._issue = ""
        decision = server.auth.check(
            self.headers.get("Host", ""), self.headers, self.client_address[0])
        if not decision.ok:
            # Identity first, always. A password cannot buy a way past this,
            # and with a public origin configured there is no exemption for
            # this machine either: the laptop uses the same private URL.
            self._drain()
            self._refuse(HTTPStatus.FORBIDDEN, decision.reason or "not allowed here")
            return

        problem = self._framing_problem()
        if problem is not None:
            status, message = problem
            # Refused before anything is forwarded: a request this gate cannot
            # frame exactly is a request it will not repeat to the console.
            self._refuse(status, message)
            self.close_connection = True
            return

        path = (self.path or "/").split("?", 1)[0]
        brand = self._brand_name(path)
        exempt = brand is not None and server.public_brand

        if not exempt:
            token = read_cookie(self.headers.get("Cookie", ""), COOKIE_NAME)
            if server.login.valid(token, decision.identity):
                # Signed in already. An Authorization header riding along is
                # not looked at, right or wrong: a client that already has a
                # login cannot lock itself out with a stale one, and a header
                # only its own browser could have sent proves nothing new.
                pass
            elif server.gate.throttled(decision.identity):
                self._drain()
                self._refuse(HTTPStatus.TOO_MANY_REQUESTS,
                             "too many failed passwords; wait a minute",
                             {"Retry-After": "60"})
                return
            elif server.gate.check(decision.identity,
                                   self.headers.get("Authorization", "")):
                # The password was right, so this browser does not have to be
                # asked again for a week. A caller that sends Basic every time
                # can ignore the cookie entirely and still work.
                self._issue = server.login.header(server.login.mint(decision.identity))
            else:
                self._drain()
                self._challenge()
                return

        if brand is not None:
            self._drain()
            self._serve_brand(brand)
            return
        if path.startswith(BRAND_PREFIX):
            # Inside the branding boundary, but not one of its files. There is
            # no directory here to list and no path to walk out of.
            self._drain()
            self._refuse(HTTPStatus.NOT_FOUND, "no such brand asset")
            return

        if path == '/api/modules' and getattr(server, 'modules', None) is not None:
            self._drain()
            if self.command not in {'GET', 'HEAD'}:
                self._refuse(405, 'Use GET for module settings'); return
            self._refuse(200, json.dumps(server.modules), content_type='application/json'); return

        if server.recovery_root and path.startswith('/api/recovery/'):
            self._recovery_control(path, decision)
            return

        if path.startswith("/api/service/") and path != "/api/service/activity":
            self._service_control(path, decision)
            return

        if getattr(server, "visualizations", None) and server.visualizations.handle(self):
            return
        if getattr(server, "workspace_api", None) and server.workspace_api.handle(self):
            return
        if getattr(server, "workspace_ui", None) and self._workspace_asset(path):
            return
        if getattr(server, "boards", None) and server.boards.handle(self):
            return
        if getattr(server, "live_agents", None) and server.live_agents.handle(self):
            return
        self._forward(server.tell if self._is_tell(path) else server.console)

    def _workspace_asset(self, path):
        # Exact release assets only; lets UI ship without restarting native workers.
        files={'/':('index.html','text/html; charset=utf-8'),
               '/index.html':('index.html','text/html; charset=utf-8'),
               '/app.js':('app.js','text/javascript; charset=utf-8'),
               '/modules.js':('modules.js','text/javascript; charset=utf-8'),
               '/styles.css':('styles.css','text/css; charset=utf-8'),
               '/workspace.js':('workspace.js','text/javascript; charset=utf-8'),
               '/tell.js':('tell.js','text/javascript; charset=utf-8'),
               '/tell.css':('tell.css','text/css; charset=utf-8'),
               '/workspace.css':('workspace.css','text/css; charset=utf-8')}
        if path not in files:return False
        self._drain()
        if self.command not in ('GET','HEAD'):
            self._refuse(HTTPStatus.METHOD_NOT_ALLOWED,'Use GET for this asset');return True
        name,mime=files[path];target=self.server.workspace_ui/name
        try:
            if target.is_symlink() or not target.is_file():raise OSError('Missing release asset')
            body=target.read_bytes()
        except OSError:
            self._refuse(HTTPStatus.SERVICE_UNAVAILABLE,'Workspace release asset unavailable');return True
        self.send_response(HTTPStatus.OK);self._emit_login()
        self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store, private');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
        return True

    def _recovery_control(self, path, decision):
        """The same external coordinator, reachable without the upstream console."""
        server = self.server
        body = None
        if self.command == 'POST':
            cookie = read_cookie(self.headers.get('Cookie', ''), COOKIE_NAME)
            csrf = self.headers.get('X-UX46-Recovery-CSRF', '')
            if (not server.login.valid(cookie, decision.identity)
                    or self.headers.get('Origin', '') != server.auth.origin_for(self.headers.get('Host', ''))
                    or not hmac.compare_digest(csrf.encode(), server.service_csrf.encode())):
                self._drain()
                self._refuse(403, json.dumps({'error':'recovery_request_forbidden'}), content_type='application/json'); return
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 < length <= 1024:
                self._drain(); self._refuse(400, 'A small JSON recovery request is required'); return
            try: body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                self._refuse(400, 'Invalid recovery request'); return
        else: self._drain()
        try:
            # Remote adapter preparation goes directly to its own console;
            # the gateway only selects registered recovery targets.
            if path not in {'/api/recovery/status','/api/recovery/start'}:
                self._refuse(404, 'No such recovery operation'); return
            identity = parse_qs(urlparse(self.path).query).get('request_id', [None])[0]
            status, payload = recovery.api(server.recovery_root, self.command, path, body, identity)
            if self.command == 'GET': payload['recovery_csrf'] = server.service_csrf
        except recovery.RecoveryBusy:
            status, payload = 409, {'error':'recovery_in_progress'}
        except (OSError, ValueError):
            status, payload = 400, {'error':'invalid_recovery','message':'Check the mode, configured agent and request ID.'}
        self._refuse(status, json.dumps(payload), content_type='application/json')

    def _service_control(self, path, decision):
        server = self.server
        if path == "/api/service/status" and self.command == "GET":
            self._drain()
            payload = dict(server.service_control.status(), csrf_token=server.service_csrf)
            self._refuse(HTTPStatus.OK, json.dumps(payload), content_type="application/json")
            return
        if path != "/api/service/restart" or self.command != "POST":
            self._drain()
            self._refuse(HTTPStatus.NOT_FOUND, "no such service operation")
            return
        # A password header alone is insufficient for this mutation. The
        # caller must hold the gate's signed cookie and its separate CSRF token.
        cookie = read_cookie(self.headers.get("Cookie", ""), COOKIE_NAME)
        origin = self.headers.get("Origin", "")
        csrf = self.headers.get("X-UX46-Service-CSRF", "")
        if (not server.login.valid(cookie, decision.identity)
                or origin != server.auth.origin_for(self.headers.get("Host", ""))
                or not hmac.compare_digest(csrf.encode("utf-8"), server.service_csrf.encode("utf-8"))):
            self._drain()
            self._refuse(HTTPStatus.FORBIDDEN, json.dumps({"error": "service_request_forbidden",
                         "message": "Sign in and reload service status before restarting."}),
                         content_type="application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 1024:
            self._drain()
            self._refuse(HTTPStatus.BAD_REQUEST, "a small JSON service request is required")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            self._refuse(HTTPStatus.BAD_REQUEST, "invalid service request JSON")
            return
        status, payload = server.service_control.restart(payload)
        self._refuse(status, json.dumps(payload), content_type="application/json")

    def _drain(self) -> None:
        """Read a refused request's body so the connection stays in step."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > MAX_UPLOAD:
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    # -- what this gate will not carry ------------------------------------
    def _framing_problem(self):
        if self.headers.get("Upgrade"):
            return (HTTPStatus.BAD_REQUEST, "this gate does not carry upgraded connections")
        if self.headers.get("Transfer-Encoding"):
            return (HTTPStatus.LENGTH_REQUIRED,
                    "send a Content-Length; this gate does not carry chunked requests")
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) > 1 and len({value.strip() for value in lengths}) > 1:
            return (HTTPStatus.BAD_REQUEST, "conflicting Content-Length")
        if lengths:
            raw = lengths[0].strip()
            if not raw.isdigit():
                return (HTTPStatus.BAD_REQUEST, "malformed Content-Length")
            if int(raw) > MAX_UPLOAD:
                return (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "that upload is too large")
        return None

    # -- branding ----------------------------------------------------------
    def _brand_name(self, path: str):
        if not path.startswith(BRAND_PREFIX):
            return None
        name = path[len(BRAND_PREFIX):]
        # An exact name from a fixed list. Not a join, not a resolve, not a
        # comparison against a root — there is nothing here to traverse.
        return name if name in BRAND_FILES else None

    def _serve_brand(self, name: str) -> None:
        root = self.server.brand_dir
        if root is None:
            self._refuse(HTTPStatus.NOT_FOUND, "no brand directory is configured")
            return
        target = root / name
        try:
            if target.is_symlink() or not target.is_file():
                raise OSError("not a plain file")
            body = target.read_bytes()
        except OSError:
            self._refuse(HTTPStatus.NOT_FOUND, "no such brand asset")
            return
        self.send_response(HTTPStatus.OK)
        self._emit_login()
        self.send_header("Content-Type", BRAND_FILES[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- forwarding --------------------------------------------------------
    def _is_tell(self, path: str) -> bool:
        return path == TELL_PREFIX or path.startswith(TELL_PREFIX + "/")

    def _forward_headers(self):
        skip = set(HOP_BY_HOP)
        for value in self.headers.get_all("Connection") or []:
            for token in value.split(","):
                skip.add(token.strip().casefold())
        # The password stops here. The console never sees it, cannot log it,
        # and does not know this gate exists.
        skip.add("authorization")
        skip.add("x-ux46-service-csrf")
        # Framing is this gate's own to state, from the bytes it actually
        # forwards, rather than copied from what the client claimed.
        skip.add("content-length")
        out = []
        for name, value in self.headers.items():
            if name.casefold() in skip:
                continue
            if name.casefold() == "cookie":
                # This gate's login is this gate's business. Every other cookie
                # the browser sent belongs to the console and goes through.
                rest = cookies_without(value, COOKIE_NAME)
                if rest:
                    out.append((name, rest))
                continue
            out.append((name, value))
        return out

    def _forward(self, upstream: Upstream) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._refuse(HTTPStatus.BAD_REQUEST, "malformed Content-Length")
            return
        connection = http.client.HTTPConnection(
            upstream.host, upstream.port, timeout=UPSTREAM_TIMEOUT)
        try:
            body = _Limited(self.rfile, length) if length else None
            connection.putrequest(self.command, self.path, skip_host=True,
                                  skip_accept_encoding=True)
            for name, value in self._forward_headers():
                connection.putheader(name, value)
            if self.headers.get("Content-Length") is not None:
                connection.putheader("Content-Length", str(length))
            connection.endheaders(message_body=body, encode_chunked=False)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            # One attempt, whatever happened. A POST that may already have been
            # applied is never sent a second time by this gate.
            connection.close()
            self._refuse(HTTPStatus.BAD_GATEWAY,
                         f"the service behind this gate did not answer: {error}")
            self.close_connection = True
            return
        try:
            self._relay(response)
        finally:
            connection.close()

    def _relay(self, response) -> None:
        skip = set(HOP_BY_HOP)
        for value in response.headers.get_all("Connection") or []:
            for token in value.split(","):
                skip.add(token.strip().casefold())
        # A provider behind the console answering 401 with its own challenge
        # would put a browser password box over UX46 for a login UX46 does not
        # hold. The status and the body go through; the challenge does not.
        skip.add("www-authenticate")
        headers = [(name, value) for name, value in response.headers.items()
                   if name.casefold() not in skip and name.casefold() != "content-length"]
        length = response.headers.get("Content-Length")
        # Older agent adapters return the complete WAV even for a Range
        # request. Adapt that authenticated response here so player upgrades
        # don't require interrupting every agent to replace its adapter.
        # Only fixed audio routes and bounded, complete representations qualify.
        is_audio = re.fullmatch(r"/api/(?:agents/[^/]+/api/)?audio/[a-zA-Z0-9_-]+\.wav", self.path)
        if (self.command == "GET" and response.status == 200 and is_audio
                and response.headers.get_content_type() == "audio/wav"
                and length and length.isdigit() and int(length) <= 64 * 1024 * 1024):
            data = response.read(int(length))
            if len(data) != int(length):
                self.close_connection = True
                self._refuse(HTTPStatus.BAD_GATEWAY, "the audio response was incomplete")
                return
            status, data, audio_headers = audio_response(data, self.headers.get("Range", ""))
            self.send_response_only(status)
            self._emit_login()
            for name, value in headers:
                if name.casefold() not in {"accept-ranges", "content-range"}:
                    self.send_header(name, value)
            for name, value in audio_headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        bodyless = (self.command == "HEAD" or response.status in (204, 304)
                    or 100 <= response.status < 200)

        self.send_response_only(response.status, response.reason)
        self._emit_login()
        for name, value in headers:
            self.send_header(name, value)
        if bodyless:
            # HEAD keeps the length it would have had; the others have none.
            if length is not None and self.command == "HEAD":
                self.send_header("Content-Length", length)
            self.end_headers()
            return
        if length is not None:
            self.send_header("Content-Length", length)
            self.end_headers()
            self._pump(response, int(length))
            return
        # No length: a stream, or a response the console closes to end. It is
        # re-framed as it arrives rather than collected first, so a long-poll
        # and a large download both reach the browser as they happen.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            chunk = response.read1(STREAM_CHUNK)
            if not chunk:
                break
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _pump(self, response, length: int) -> None:
        remaining = length
        while remaining > 0:
            chunk = response.read(min(STREAM_CHUNK, remaining))
            if not chunk:
                # The console promised more than it sent. Ending the connection
                # is the only honest way to say the body is short.
                self.close_connection = True
                return
            self.wfile.write(chunk)
            self.wfile.flush()
            remaining -= len(chunk)

    # Every method the console and Tell use arrives at the same door.
    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_one


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, gate: Gate, auth: AuthBoundary, console: Upstream,
                 tell: Upstream, brand_dir: Path = None, public_brand: bool = False,
                 verbose: bool = False, login: "Login" = None, service_control=None, recovery_root=None):
        if address[0] != "127.0.0.1":
            raise ValueError("this gate binds 127.0.0.1 only")
        super().__init__(address, GatewayHandler)
        self.gate = gate
        self.login = login or Login(gate.stored)
        self.auth = auth
        self.console = console
        self.tell = tell
        self.brand_dir = brand_dir
        self.public_brand = public_brand
        self.gateway_verbose = verbose
        self.service_csrf = secrets.token_urlsafe(32)
        self.service_control = service_control or ServiceControl(console, auth)
        self.recovery_root = Path(recovery_root).resolve() if recovery_root else None

    def handle_error(self, request, client_address):
        # A browser that walked away mid-download is not an incident, and a
        # traceback here could quote a request. Say one line or nothing.
        if self.gateway_verbose:
            kind = sys.exc_info()[0]
            sys.stderr.write(f"gate: dropped a request ({kind.__name__ if kind else 'unknown'})\n")


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A password in front of the private UX46 console.")
    parser.add_argument("--set-password", action="store_true",
                        help="prompt for a password and write its hash, then exit")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--public-origin", default="",
                        help="the exact private origin the HTTPS front serves")
    parser.add_argument("--public-user", default="",
                        help="the exact identity that front verifies")
    parser.add_argument("--password-file", default="",
                        help="where the password hash is kept (mode 600)")
    parser.add_argument("--username", default=default_username(),
                        help="the name the browser prompt asks for; defaults to this "
                             "account. No password ships: set one before first use.")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM,
                        help="the console, on loopback")
    parser.add_argument("--tell-upstream", default=DEFAULT_TELL_UPSTREAM,
                        help="Tell, on loopback")
    parser.add_argument("--brand-dir", default=str(HERE.parent / "app" / "console" / "brand"))
    parser.add_argument("--public-brand", action="store_true",
                        help="serve the fixed brand files without a password, after the "
                             "identity check (for home-screen icons and the manifest)")
    parser.add_argument("--additional-agents-config", default="", help="Reloadable loopback adapter registry")
    parser.add_argument("--workspace-store", default="", help="Opt-in private email and Constellation store directory")
    parser.add_argument("--constellation-client", default="", help="Explicit scoped client config for shared Constellation")
    parser.add_argument("--visualization-root", action="append", default=[], help="Explicit directory of local HTML visualization fragments")
    parser.add_argument("--visualization-kit", default="", help="Installed visualization assets directory")
    parser.add_argument("--workspace-ui-dir", default="", help="Opt-in fixed UI assets; independent of native workers")
    parser.add_argument("--modules-config", default="", help="Optional private JSON of enabled email, tell and constellation modules")
    parser.add_argument("--recovery-root", default="", help="Explicit private UX46 installation whose recovery stays reachable without the console")
    parser.add_argument("--boards-store", default=str(Path.home() / ".local/state/ux46/boards.sqlite3"),
                        help="Owner conversation boards SQLite store")
    parser.add_argument("--identity-header", default="Tailscale-User-Login")
    parser.add_argument("--verbose", action="store_true",
                        help="one line per request: method, path and status, never a header")
    return parser


def set_password_flow(args) -> int:
    if not args.password_file:
        print("--password-file is required with --set-password", file=sys.stderr)
        return 2
    first = getpass.getpass(f"New UX46 password for {args.username}: ")
    if not first.strip():
        print("An empty password is not a password. Nothing was written.", file=sys.stderr)
        return 2
    again = getpass.getpass("Again: ")
    if first != again:
        print("Those did not match. Nothing was written.", file=sys.stderr)
        return 2
    path = Path(args.password_file).expanduser()
    try:
        write_password(path, args.username, first)
    except (PasswordFileError, OSError) as error:
        print(f"Could not write it: {error}", file=sys.stderr)
        return 1
    print(f"Wrote the hash to {path} (mode 600). Restart the gate to use it.")
    return 0


def serve(args) -> int:
    if not args.public_origin or not args.public_user:
        print("--public-origin and --public-user are required: this gate is only "
              "ever in front of a private origin.", file=sys.stderr)
        return 2
    if not args.password_file:
        print("--password-file is required.", file=sys.stderr)
        return 2
    if not scrypt_available():
        print("Refusing to start: this Python has no hashlib.scrypt (it is built "
              "against LibreSSL). Use a Python built against OpenSSL 1.1 or later.",
              file=sys.stderr)
        return 2
    try:
        stored = load_password(Path(args.password_file).expanduser())
    except PasswordFileError as error:
        print(f"Refusing to start: {error}", file=sys.stderr)
        return 2
    if stored.username != args.username:
        print(f"Refusing to start: that password file is for {stored.username!r}, "
              f"not {args.username!r}.", file=sys.stderr)
        return 2
    try:
        console = Upstream(args.upstream)
        tell = Upstream(args.tell_upstream)
    except ValueError as error:
        print(f"Refusing to start: {error}", file=sys.stderr)
        return 2

    brand_dir = Path(args.brand_dir).expanduser() if args.brand_dir else None
    auth = AuthBoundary(port=args.port, public_origin=args.public_origin,
                        public_user=args.public_user,
                        identity_header=args.identity_header)
    server = GatewayServer(("127.0.0.1", args.port), Gate(stored, args.username), auth,
                           console, tell, brand_dir, args.public_brand, args.verbose,
                           recovery_root=getattr(args, 'recovery_root', '') or None)
    server.modules = None
    if args.modules_config:
        try:
            path = Path(args.modules_config).expanduser()
            if path.stat().st_size > 65536: raise ValueError('too large')
            modules = json.loads(path.read_text())
            if (not isinstance(modules, dict) or set(modules) - {'email', 'tell', 'constellation'}
                    or any(type(value) is not bool for value in modules.values())):
                raise ValueError('invalid module settings')
            server.modules = dict(email=False, tell=False, constellation=True) | modules
        except (OSError, ValueError):
            parser.error('--modules-config must contain only boolean email, tell and constellation settings')
    from ux46_board_gateway import BoardAPI
    server.boards = BoardAPI(args.boards_store)
    if args.workspace_store:
        from ux46_workspace_api import WorkspaceAPI
        server.workspace_api = WorkspaceAPI(args.workspace_store,args.constellation_client or None, modules=server.modules)
    if args.visualization_root:
        if not args.visualization_kit:parser.error('--visualization-root requires --visualization-kit')
        from ux46_visualizations import Visualizations
        server.visualizations = Visualizations(args.visualization_root, args.visualization_kit)
    if args.workspace_ui_dir:
        if not args.workspace_store:parser.error('--workspace-ui-dir requires --workspace-store')
        server.workspace_ui = Path(args.workspace_ui_dir).expanduser().resolve()
    if args.additional_agents_config:
        from ux46_live_agents import LiveAgents
        server.live_agents = LiveAgents(args.additional_agents_config)
    print(f"UX46 access gate on 127.0.0.1:{server.server_address[1]} — "
          f"{args.public_origin} only, for {args.public_user}, with a password.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.set_password:
        return set_password_flow(args)
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
