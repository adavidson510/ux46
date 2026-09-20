"""Configure a locally owned installation without reading provider credentials."""
import json
import os
from pathlib import Path
import shutil
import sys

PROVIDER_DOCS = {'codex':'https://developers.openai.com/codex/cli',
                 'claude':'https://code.claude.com/docs/en/setup'}


def detected():
    # Finding an executable answers "installed?", not "signed in?". Provider
    # authentication stays with the provider; setup never reads its credentials.
    return {name: shutil.which(name) for name in PROVIDER_DOCS}


def save(path, value):
    # Write a complete replacement beside the current file, then rename it.
    # Readers should see the old JSON or the new JSON, not half a write. This
    # does not coordinate two simultaneous writers; setup has one writer.
    temporary=path.with_suffix('.pending')
    with temporary.open('w') as out:
        os.chmod(temporary,0o600)
        json.dump(value,out,indent=2);out.write('\n')
    temporary.replace(path)


def configure(root, config, *, agent='auto', name=None, cli=None):
    choices=detected()
    if agent=='auto':
        found=[k for k,v in choices.items() if v]
        if len(found)>1:raise ValueError('Both CLIs found. Choose --agent codex or --agent claude (or none).')
        agent=found[0] if found else 'none'
    if agent not in ('codex','claude','none'):raise ValueError('Unsupported local agent')
    if cli and agent=='none':raise ValueError('--cli needs a native agent')
    if name is not None and (not name.strip() or len(name)>48):raise ValueError('Name must be 1–48 characters')
    chosen=shutil.which(cli or agent) if agent!='none' else None
    if cli and not chosen:raise ValueError('The specified CLI executable was not found')
    config=dict(config, agent=agent, agent_label=name or {'codex':'Codex','claude':'Claude','none':'Connect an agent'}[agent])
    config['cli']=chosen or (agent if agent!='none' else '')
    save(root/'config.json',config)
    from ux46_doctor import runtime_check
    readiness = {'command':runtime_check(config), 'native_file_edit':'unverified',
                 'message':'Command checks do not prove a native task can edit a file. Verify that through requested work in a disposable project.'}
    source=Path(__file__).resolve().parents[1]
    # A small map for the agent's next context window. Command arrays keep
    # paths (including spaces) as arguments rather than executable shell text.
    # The environment points memory tools at this copy's private data.
    connection={'schema_version':1,'agent':agent,'name':config['agent_label'],
        'cli_available':bool(chosen),'login':'provider-managed; not inspected','readiness':readiness,
        'source':str(source),'data':str(root),'registry':str(root/'registry.json'),
        'launch':[sys.executable,str(source/'tools/ux46'),'run'],
        'memory':[sys.executable,str(source/'tools/ux46_memory.py')],
        'vault':[sys.executable,str(source/'tools/session_vault.py'),'--registry',str(root/'registry.json')],
        'instructions':str(source/'docs/ai-install.md'),
        'environment':{'UX46_HOME':str(root),'ATLAS_REGISTRY':str(root/'registry.json')},
        'provider_setup':PROVIDER_DOCS.get(agent), 'network_sharing':False}
    save(root/'connection.json',connection)
    return connection


def connect(root, *, identity, name, runtime, port):
    import re
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',identity) or identity=='local':raise ValueError('Choose a short agent ID other than local')
    if not name or len(name)>48 or not re.fullmatch(r'[a-z][a-z0-9._-]{0,63}',runtime):raise ValueError('Invalid name or runtime')
    if not 1<=port<=65535:raise ValueError('Invalid port')
    path=root/'agents.json';payload=json.loads(path.read_text())
    if any(a['id']==identity for a in payload['agents']):raise ValueError('Agent ID already configured; edit its local entry to change it')
    payload['agents'].append({'id':identity,'label':name,'runtime':runtime,'node':'local',
                             'transport':'loopback','remote_port':port})
    save(path,payload)
    # Registration records an intended connection. Only a later request to the
    # adapter can tell us whether it is actually reachable and compatible.
    return {'id':identity,'configured':True,'verified':False,'restart_required':True}
