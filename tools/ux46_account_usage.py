"""Read-only native allowance snapshot. Never starts a thread or spends/reset credits."""
from __future__ import annotations
import argparse
import json
import math
import selectors
import subprocess
import time


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def project_usage(payload, now=None):
    """Only expose documented numeric quota fields, not account IDs or tokens."""
    buckets = payload.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        value = payload.get('rateLimits')
        buckets = {value.get('limitId') or 'codex': value} if isinstance(value, dict) else {}
    result = {'state': 'reported' if buckets else 'unavailable', 'checked_at': now or time.time(), 'buckets': []}
    if type(payload.get('ordinaryUsageAllowed')) is bool:
        result['ordinary_usage_allowed'] = payload['ordinaryUsageAllowed']
    for key, value in list(buckets.items())[:20]:
        if not isinstance(value, dict): continue
        row = {'id': str(key)[:100], 'windows': []}
        for name in ('primary', 'secondary'):
            window = value.get(name)
            if not isinstance(window, dict): continue
            part = {'name': name}
            percent = window.get('usedPercent')
            if finite(percent) and 0 <= percent <= 100: part['used_percent'] = percent
            duration = window.get('windowDurationMins')
            if finite(duration) and duration > 0: part['minutes'] = duration
            reset = window.get('resetsAt')
            if finite(reset) and reset > 0: part['reset_at'] = reset
            row['windows'].append(part)
        credits = value.get('credits')
        if isinstance(credits, dict):
            row['credits_available'] = credits.get('hasCredits') is True or credits.get('unlimited') is True
        if type(value.get('spendControlReached')) is bool: row['spend_control_reached'] = value['spendControlReached']
        result['buckets'].append(row)
    credits = payload.get('rateLimitResetCredits')
    if isinstance(credits, dict) and type(credits.get('availableCount')) is int:
        result['available_resets'] = max(0, credits['availableCount'])
    return result


def probe(codex):
    # A separate short-lived metadata connection uses this host's saved login.
    # No auth file is opened here; credentials remain with the native runtime.
    proc = subprocess.Popen([codex, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector(); selector.register(proc.stdout, selectors.EVENT_READ)
    buffered = b''
    deadline = time.monotonic() + 12
    def request(identifier, method, params):
        nonlocal buffered
        proc.stdin.write((json.dumps({'id': identifier, 'method': method, 'params': params})+'\n').encode()); proc.stdin.flush()
        while time.monotonic() < deadline:
            while b'\n' in buffered:
                line, buffered = buffered.split(b'\n', 1)
                message = json.loads(line)
                if message.get('id') == identifier:
                    if 'error' in message: raise ValueError('Native usage unavailable')
                    return message.get('result') or {}
            if selector.select(max(0, deadline-time.monotonic())):
                import os
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk: break
                buffered += chunk
                if len(buffered) > 1024*1024: raise ValueError('Native reply too large')
        raise TimeoutError('Native usage timed out')
    try:
        request(1, 'initialize', {'clientInfo': {'name':'ux46_usage', 'version':'1'}, 'capabilities':{'experimentalApi':True}})
        proc.stdin.write(b'{"method":"initialized","params":{}}\n');proc.stdin.flush()
        return project_usage(request(2, 'account/rateLimits/read', {}))
    finally:
        selector.close();proc.terminate()
        try:proc.wait(timeout=2)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        proc.stdin.close();proc.stdout.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--codex',default='codex');args=parser.parse_args()
    try:result=probe(args.codex)
    except Exception:result={'state':'unavailable','checked_at':time.time(),'buckets':[]}
    print(json.dumps(result))
