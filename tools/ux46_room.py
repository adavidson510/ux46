"""Change a conversation's shared tab label or mark without changing its identity."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from atlas_desktops import DesktopStore, Conflict


def reference(agent, room):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', agent) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96}', room):
        raise ValueError('Use the actual UX46 agent ID and project/session')


def presentation(state, agent, room):
    def exact(entry):return entry.get('agent') == agent and entry.get('room') == room
    return {'label':next((a.get('label') for a in state.get('aliases',[]) if exact(a)),None),
            'mark':next((a for a in state.get('sessionMarks',[]) if exact(a)),None)}


def change(store, agent, room, version, kind, value, file_agent=None):
    reference(agent,room)
    latest=store.read()
    if latest['version'] != version: raise Conflict(latest)
    state=latest['state']
    def exact(entry):return entry.get('agent') == agent and entry.get('room') == room
    if kind == 'label':
        if not isinstance(value,str) or not 1 <= len(value.strip()) <= 60 or any(ord(c)<32 for c in value):
            raise ValueError('Use a label of 1–60 characters')
        found=next((a for a in state['aliases'] if exact(a)),None)
        if found: found['label']=value.strip()
        else: state['aliases'].append({'agent':agent,'room':room,'label':value.strip()})
    else:
        if kind == 'brand' and value != 'ux46': raise ValueError('The available built-in brand is ux46')
        if kind == 'initials' and (not 1 <= len(value) <= 4 or any(ord(c)<33 for c in value)):
            raise ValueError('Use 1–4 visible characters')
        if kind == 'file':
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value): raise ValueError('Use an uploaded file ID, not a path or URL')
            reference(file_agent or agent,room)
        if kind not in {'brand','initials','file','reset'}: raise ValueError('Unknown mark kind')
        marks=[m for m in state.get('sessionMarks',[]) if not exact(m)]
        if kind != 'reset':
            mark={'agent':agent,'room':room,'kind':kind,'value':value,
                  'updated_at':datetime.now(timezone.utc).isoformat()}
            if kind == 'file': mark['file_agent']=file_agent or agent
            marks.append(mark)
        state['sessionMarks']=marks
    result=store.save(version,state)
    return {'version':result['version'],**presentation(result['state'],agent,room)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store',type=Path,default=Path.home()/'.local/state/atlas-console-v1/desktops.sqlite3')
    p.add_argument('--agent',required=True);p.add_argument('--room',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('get')
    rename=sub.add_parser('rename');rename.add_argument('label');rename.add_argument('--base-version',type=int,required=True)
    icon=sub.add_parser('icon');icon.add_argument('--base-version',type=int,required=True)
    group=icon.add_mutually_exclusive_group(required=True)
    group.add_argument('--brand');group.add_argument('--initials');group.add_argument('--file-id');group.add_argument('--reset',action='store_true')
    icon.add_argument('--file-agent')
    args=p.parse_args()
    try:
        reference(args.agent,args.room)
        if not args.store.is_file():raise ValueError('Select the existing UX46 owner store; no workspace was created')
        store=DesktopStore(args.store)
        if args.command == 'get':
            x=store.read();result={'version':x['version'],**presentation(x['state'],args.agent,args.room)}
        elif args.command == 'rename': result=change(store,args.agent,args.room,args.base_version,'label',args.label)
        else:
            kind,value=next((k,v) for k,v in [('brand',args.brand),('initials',args.initials),('file',args.file_id),('reset',args.reset)] if v)
            result=change(store,args.agent,args.room,args.base_version,kind,value,args.file_agent)
        print(json.dumps(result,ensure_ascii=False));return 0
    except Conflict:
        print(json.dumps({'error':'workspace_conflict','message':'Workspace changed. Read again before applying your change.'}));return 2
    except (ValueError,OSError) as exc:p.error(str(exc))


if __name__=='__main__':raise SystemExit(main())
