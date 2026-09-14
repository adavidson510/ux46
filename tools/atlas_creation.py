"""Small shared contract for explicit new conversations, with no model call."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import io
import re
import time
from pathlib import Path
import session_vault as vault


def request(body, projects):
    client = body.get('client_id')
    title = body.get('title', '')
    wanted = body.get('project_id')
    if (not isinstance(client,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}',client)
            or not isinstance(title,str) or len(title)>120
            or (wanted is not None and not isinstance(wanted,str))):
        raise ValueError('Choose an agent, a project or blank, and a short name.')
    project = projects.get(wanted) if wanted else None
    if wanted and (not project or not project.get('root') or not Path(project['root']).expanduser().is_dir()):
        raise ValueError('That project is not available on this agent.')
    title = title.strip() or ('New '+project['name']+' conversation' if project else 'Exploration')
    return client, title, project


def options(projects):
    return {'available':True,'blank':True,'projects':[
        {'id':p['id'],'name':p['name']} for p in projects.values()
        if p.get('root') and Path(p['root']).expanduser().is_dir()]}


def link(project, title, client, runtime, session_id, node, cwd):
    """File only the freshly created native origin, never the controller's."""
    if not project:
        return
    p=vault.Project(project['id'],project['name'],Path(project['root']).expanduser(),())
    p.sessions_dir.mkdir(parents=True,exist_ok=True)
    stem=re.sub(r'[^a-z0-9]+','-',title.casefold()).strip('-')[:42] or 'exploration'
    session=stem+'-'+hashlib.sha256(client.encode()).hexdigest()[:16]
    args=argparse.Namespace(identity=p.id+'/'+session,title=title,
        summary='New conversation created in UX46. Project home: '+str(p.root)+'. Use Session Vault for focused recall; no prior transcript was inherited.',
        keywords=p.id+', exploration',status='active',no_link_current=True,json=True,
        primary=False,runtime=None,session_id=None,session_name=None,node=None,cwd=None)
    with contextlib.redirect_stdout(io.StringIO()):vault.command_start(args,[p],node)
    record=vault.parse_record(p,p.sessions_dir/(session+'.md'))
    stamp=time.strftime('%Y-%m-%d')
    vault.link_origin(record,{'runtime':runtime,'node':node,'session_id':session_id,
        'session_name':'','cwd':cwd,'captured':stamp,'last_seen':stamp,'primary':True},make_primary=True)
