"""Bounded, redacted installation diagnostics and explicit recovery entrypoints."""
from __future__ import annotations
import http.client
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from ux46_recovery import Client, Coordinator, ROOM, ServiceManager, installation, read_json


def runtime_check(config, runner=subprocess.run):
    provider = config['agent']
    if provider == 'none': return {'provider': 'none', 'state': 'skipped', 'account': 'unknown', 'quota': 'unknown'}
    executable = shutil.which(config.get('cli') or provider)
    result = {'provider': provider, 'state': 'missing', 'executable_found': bool(executable),
              'configured_executable': config.get('cli') or provider, 'version': None, 'capability': 'unknown',
              'account': 'unknown', 'quota': 'unknown'}
    if not executable: return result
    try:
        version = runner([executable, '--version'], capture_output=True, text=True, timeout=3, check=False)
        # Native output can include local details. Keep only the version token;
        # never echo a CLI error, identity, account ID or credential contents.
        match = re.search(r'\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b', version.stdout[:4096])
        result['version'] = match[0] if match else None
        result['state'] = 'available' if version.returncode == 0 else 'unavailable'
        if provider == 'codex':
            capability = runner([executable, 'app-server', '--help'], capture_output=True, text=True, timeout=3, check=False)
            result['capability'] = 'app-server' if capability.returncode == 0 and 'app-server' in capability.stdout else 'unsupported'
        else:
            lsof = shutil.which('lsof') or ('/usr/sbin/lsof' if Path('/usr/sbin/lsof').is_file() else None)
            result['lsof'] = bool(lsof)
            inventory = runner([executable, 'agents', '--json'], capture_output=True, text=True, timeout=5, check=False)
            try: valid = isinstance(json.loads(inventory.stdout), (dict, list)) and inventory.returncode == 0
            except ValueError: valid = False
            result['capability'] = 'native-inventory' if valid and lsof else 'prerequisite_missing'
        if result['capability'] in {'unsupported', 'prerequisite_missing'}: result['state'] = 'incompatible'
    except (OSError, subprocess.SubprocessError): result['state'] = 'unavailable'
    return result


def inspect(root, agent=None, room=None, *, client_factory=Client, runner=subprocess.run):
    report = {'schema_version': 1, 'checked_at': time.time(), 'inspection_only': True,
              'python': sys.version.split()[0], 'platform': sys.platform,
              'configuration': 'invalid', 'services': [], 'agents': [], 'account': 'unknown', 'quota': 'unknown',
              'actions': ['ux46 doctor --agent ID --recover', 'ux46 doctor --recover-all']}
    if room and not ROOM.fullmatch(room): raise ValueError('Use an exact project/session room')
    try: plan = installation(root)
    except (ValueError, OSError, TypeError):
        report['message'] = 'Configuration is missing or invalid. Run ux46 setup, or repair the private configuration.'
        return report
    report['configuration'] = 'valid'
    chosen = plan['agents'] if not agent else [a for a in plan['agents'] if a['id'] == agent]
    if agent and not chosen: raise ValueError('Agent is not configured in this installation')
    if room and not agent: raise ValueError('--room needs --agent')
    report['runtime'] = runtime_check(plan['config'], runner)
    deadline = time.monotonic()+15
    manager = ServiceManager(plan['root'], deadline)
    for service in plan['services']:
        try:
            status = manager.inspect(service)
            state = 'running' if status.get('running') else 'unsupported' if not status.get('supported') else 'stopped'
            if status.get('replaced'): state = 'ownership_changed'
        except (OSError, ValueError, subprocess.SubprocessError): state = 'unknown'
        report['services'].append({'id': service['id'], 'state': state})
    if not plan['services']: report['services'].append({'id': 'console', 'state': 'unregistered'})
    # A skipped provider still has a workspace worth checking.
    for target in chosen or ([{'id':'workspace','runtime':'none','endpoint':plan['console']}] if not agent else []):
        row = {'id': target['id'], 'runtime': target['runtime'], 'transport': 'unknown', 'account': 'unknown', 'quota': 'unknown'}
        report['agents'].append(row)
        if target.get('unsupported') or not target.get('endpoint'):
            row['transport'] = 'unsupported'; continue
        client = client_factory(target['endpoint'], deadline)
        try:
            status, boot = client.request('GET', '/api/bootstrap')
            row['transport'] = 'reachable' if status == 200 else 'unavailable'
            row['recovery_supported'] = bool(boot.get('recovery', {}).get('admission'))
            if status != 200: continue
            if room:
                status, detail = client.request('GET', '/api/room/'+room+'?inspect=1')
                if status != 200: row['room'] = {'state': 'unavailable'}; continue
                native = detail.get('native') or {}; account = detail.get('account_status') or {}
                current = account.get('checked_at')
                account_state = account.get('state') if isinstance(current, (int, float)) and 0 <= time.time()-current <= 90 else 'unknown'
                if account_state not in {'available','limited','sign_in_required'}: account_state = 'unknown'
                row['account'] = 'sign_in_required' if account_state == 'sign_in_required' else 'unknown'
                row['quota'] = account_state if account_state in {'available','limited'} else 'unknown'
                row['room'] = {'state': 'observed', 'observed_at': time.time(), 'active_turn': bool(native.get('active_turn')),
                    'ownership': detail.get('ownership', {}).get('state', 'unknown'),
                    'pending_approvals': len(detail.get('approvals') or []),
                    'historical_failure': (detail.get('native_terminal') or {}).get('kind') in {'usage_limit','auth','failed'}}
        except (OSError, ValueError, TypeError, http.client.HTTPException): row['transport'] = 'unavailable'
    saved = Coordinator(root).receipt()
    if saved:
        report['last_recovery'] = {key: saved.get(key) for key in ('id','mode','agent','state','at','finished_at')}
    report['message'] = 'Provider sign-in stays on the agent host. Refresh uses its saved login; it cannot reset quota or resend input.'
    return report


def successful(report):
    return report['configuration'] == 'valid' and report.get('runtime', {}).get('state') in {'available','skipped'} and all(a['transport'] == 'reachable' and a['account'] != 'sign_in_required' and a['quota'] != 'limited' for a in report['agents']) and all(s['state'] == 'running' for s in report['services'])


def render(report):
    print('UX46 doctor · inspection only')
    print('Configuration: ' + report['configuration'])
    runtime = report.get('runtime') or {}
    if runtime: print('Runtime: ' + runtime.get('provider','') + ' · ' + runtime.get('state','unknown') + (' · '+runtime['version'] if runtime.get('version') else ''))
    for row in report['services']: print('Service '+row['id']+': '+row['state'])
    for row in report['agents']: print('Agent '+row['id']+': '+row['transport']+' · current quota '+row['quota'])
    print(report.get('message',''))
    print('Refresh one agent: ux46 doctor --agent ID --recover')
    print('Full recovery: ux46 doctor --recover-all (may interrupt this installation’s active owned work)')
