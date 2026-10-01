"""Optional fixed per-agent quota probes, independent of active native workers."""
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from ux46_live_agents import LiveAgents


class AccountUsageAPI:
    def __init__(self, config_path):
        config=json.loads(Path(config_path).read_text())
        self.commands=config['agents'];self.local=config.get('local_agent','local')
        if not isinstance(self.commands,dict) or any(not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', k)
            or not isinstance(v,list) or not v or any(not isinstance(x,str) or not x for x in v)
            for k,v in self.commands.items()):raise ValueError('Invalid fixed usage commands')
        self.cache={};self.locks={key:threading.Lock() for key in self.commands}
        self.io=LiveAgents.__new__(LiveAgents)

    def read(self, agent):
        # Deduplicate windows/tabs, bound subprocess lifetime, never shell-evaluate.
        with self.locks[agent]:
            prior=self.cache.get(agent)
            if prior and time.monotonic()-prior[0]<15:return prior[1]
            try:
                run=subprocess.run(self.commands[agent],capture_output=True,timeout=18,check=True)
                if len(run.stdout)>65536:raise ValueError('Oversized probe')
                result=json.loads(run.stdout)
                if result.get('state') not in ('reported','unavailable') or not isinstance(result.get('buckets'),list):raise ValueError('Invalid probe')
            except (OSError,ValueError,subprocess.SubprocessError):
                result={'state':'unavailable','checked_at':time.time(),'buckets':[]}
            result['source']='saved_login';self.cache[agent]=(time.monotonic(),result)
            return result

    def handle(self, handler):
        match=re.fullmatch(r'(?:/api/agents/([a-z][a-z0-9-]{0,31}))?/api/account-usage',handler.path)
        if not match or handler.command!='GET':return False
        agent=match[1] or self.local
        if agent not in self.commands:return False
        # The access gateway's identity/password gate runs before this handler.
        self.io._reply(handler,200,self.read(agent));return True
