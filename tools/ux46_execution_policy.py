"""Owner-selected native access defaults, stored separately from conversations.

Changing a default does not mutate running workers. They adopt it on their next
explicit reconnect, and native readback must still verify the requested access.
"""
import json
import os
from pathlib import Path
import threading
import atlas_native_profile as profiles

CHOICES = [
    {'id': 'workspace-write', 'label': 'Project access',
     'description': 'Write in the project and temporary folders. Ask before commands need broader access.'},
    {'id': 'full-access', 'label': 'Full access (YOLO)',
     'description': 'Use the files, commands and network available to this OS account, without routine approval prompts.'},
    {'id': 'preserve', 'label': 'Keep CLI / session settings',
     'description': 'New sessions use CLI defaults; existing sessions keep their recorded permissions.'},
]

class PolicyConflict(ValueError):
    pass

class ExecutionPolicy:
    def __init__(self, path, default):
        if default not in profiles.POLICIES: raise ValueError('Unknown access policy')
        self.path = Path(path)
        self.lock = threading.RLock()
        self.current = {'policy': default, 'revision': 0}
        if self.path.exists():
            raw = self.path.read_text()
            if len(raw) > 4096: raise ValueError('Access policy file is too large')
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) != {'policy', 'revision'}
                    or value['policy'] not in profiles.POLICIES
                    or type(value['revision']) is not int or value['revision'] < 0):
                raise ValueError('Invalid saved access policy; repair the policy file before starting')
            self.current = value

    def read(self):
        with self.lock:
            return dict(self.current, choices=CHOICES, applies='new-and-reconnected-sessions')

    def set(self, policy, revision):
        if policy not in profiles.POLICIES or type(revision) is not int:
            raise ValueError('Choose a supported access policy')
        with self.lock:
            if revision != self.current['revision']:
                raise PolicyConflict('The access default changed elsewhere. Reload it before saving.')
            if policy == self.current['policy']: return self.read()
            value = {'policy': policy, 'revision': revision + 1}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(value, f);f.write('\n');f.flush();os.fsync(f.fileno())
            temp.replace(self.path)
            self.current = value
            return self.read()
