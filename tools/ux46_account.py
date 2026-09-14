"""Bounded account observations, separate from a previous turn's result.

No model calls, login mutations, resets, credentials in output, or prompt retry.
"""
from __future__ import annotations
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time


def login_identity(home: Path | None = None) -> str | None:
    """Opaque, process-local identity; token rotation alone isn't a login change.

    Managed file authentication is observable. Keychain/unknown stores remain
    unknown and retain explicit refresh. Never publish this digest or its inputs.
    """
    root = home or Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    try:
        with (root / 'auth.json').open('rb') as f:
            raw = f.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        tokens = value.get('tokens') or {}
        if not isinstance(tokens, dict):
            return None
        subject = ''
        token = tokens.get('id_token')
        if isinstance(token, str) and token.count('.') == 2:
            encoded = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if isinstance(claims, dict): subject = claims.get('sub') or ''
        parts = [value.get('auth_mode'), tokens.get('account_id'), subject, value.get('OPENAI_API_KEY')]
        if not any(parts): return None
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    except FileNotFoundError:
        return 'missing'
    except (OSError, ValueError, TypeError):
        return None


def project_limits(payload: dict, now: float) -> dict:
    result = {'state': 'unknown', 'checked_at': now}
    limits = payload.get('rateLimits')
    buckets = payload.get('rateLimitsByLimitId')
    if isinstance(buckets, dict) and isinstance(buckets.get('codex'), dict):
        limits = buckets['codex']
    if not isinstance(limits, dict): return result
    windows = [limits.get(key) for key in ('primary', 'secondary')]
    windows = [w for w in windows if isinstance(w, dict)]
    valid = [w for w in windows if type(w.get('usedPercent')) in (int, float)
             and math.isfinite(w['usedPercent']) and 0 <= w['usedPercent'] <= 100]
    reached = bool(limits.get('rateLimitReachedType'))
    exhausted = [w for w in valid if w['usedPercent'] >= 100]
    credits = limits.get('credits') or {}
    can_pay = isinstance(credits, dict) and (credits.get('unlimited') is True or credits.get('hasCredits') is True)
    if reached or (exhausted and not can_pay):
        result['state'] = 'limited'
        resets = [w.get('resetsAt') for w in exhausted if type(w.get('resetsAt')) in (int, float)
                  and math.isfinite(w['resetsAt']) and w['resetsAt'] > now]
        if resets: result['reset_at'] = max(resets)
    elif valid and len(valid) == len(windows) and all(w['usedPercent'] < 100 for w in valid):
        result['state'] = 'available'
    return result


class AccountStatus:
    def __init__(self, server, ttl=30.0):
        self.server, self.ttl = server, ttl
        self._lock = threading.Lock()
        self._pending = False
        self._result = {'state': 'unknown', 'checked_at': None}
        self._expires = 0.0
        self._generation = 0

    def invalidate(self, clear=False):
        with self._lock:
            self._generation += 1
            self._expires = 0.0
            if clear:
                self._result = {'state': 'unknown', 'checked_at': None}

    def read(self):
        now = time.monotonic()
        with self._lock:
            if now >= self._expires and not self._pending:
                self._pending = True
                threading.Thread(target=self._check, name='ux46-account-status', daemon=True).start()
            result = dict(self._result)
            # A stale healthy observation must not indefinitely hide a new failure.
            if result.get('checked_at') and time.time() - result['checked_at'] > max(self.ttl * 3, 90):
                return {'state': 'unknown', 'checked_at': result['checked_at']}
            return result

    def _check(self):
        with self._lock:
            generation = self._generation
        result = {'state': 'unknown', 'checked_at': time.time()}
        try:
            account = self.server.request('account/read', {'refreshToken': False}, timeout=3)
            if account.get('requiresOpenaiAuth') and not account.get('account'):
                result['state'] = 'sign_in_required'
            elif isinstance(account.get('account'), dict) and account['account'].get('type') in ('chatgpt', 'chatgptAuthTokens'):
                limits = self.server.request('account/rateLimits/read', {}, timeout=3)
                result = project_limits(limits, time.time())
        except Exception:
            # Credentials/raw provider errors never leave this observation layer.
            pass
        finally:
            with self._lock:
                if generation == self._generation:
                    self._result = result
                    self._expires = time.monotonic() + self.ttl
                self._pending = False
