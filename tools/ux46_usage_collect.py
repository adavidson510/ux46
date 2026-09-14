"""Refresh explicit UX46 desktop usage via existing read APIs, without agent turns."""
import argparse
import concurrent.futures
import json
import os
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit


def request(config, endpoint, path):
    origin=endpoint['url']; parsed=urlsplit(origin)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost'):
        raise ValueError('Only configured local adapter/proxy endpoints are allowed')
    req=Request(origin+path, headers=config.get('headers',{}))
    with urlopen(req,timeout=8) as response:
        raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('Usage response bound exceeded')
        return json.loads(raw)


def desktop_rooms(state):
    tabs=list(state.get('sharedLayout',{}).get('tabs',[]))
    for desk in state.get('desktops',[]):tabs.extend(desk.get('tabs',[]))
    for desk in state.get('liveDesktops',[]):tabs.extend(desk.get('layout',{}).get('tabs',[]))
    if state.get('uiSession'):tabs.append(state['uiSession'])
    return sorted({(t.get('agent','local'),t['room']) for t in tabs if isinstance(t,dict) and
        re.fullmatch(r'[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96}',t.get('room',''))})


def collect(config, directory):
    root=Path(directory);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    state_path=root/'usage-collection.json';previous={}
    try:previous=json.loads(state_path.read_text())
    except (OSError,ValueError):pass
    endpoints={e['id']:e for e in config['agents']}
    state=request(config,config['desktop'],'/api/desktop-state')['state']
    rooms=desktop_rooms(state);supported=[r for r in rooms if endpoints.get(r[0],{}).get('runtime')=='codex']
    offset=previous.get('next_offset',0)%max(1,len(supported))
    selected=(supported[offset:]+supported[:offset])[:24]
    def refresh(room):
        agent,rid=room;endpoint=endpoints[agent]
        try:
            result=request(config,endpoint,endpoint.get('prefix','')+'/api/room/'+rid+'/usage')
            return {'agent':agent,'room':rid,'state':'indexed' if 'usage' in result else 'unavailable',
                    'pending':bool(result.get('incremental',{}).get('pending'))}
        except Exception:return {'agent':agent,'room':rid,'state':'unavailable'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        receipts=list(pool.map(refresh,selected))
    responses=[]
    for endpoint in endpoints.values():
        agent={k:endpoint[k] for k in ('id','label','runtime')}
        if endpoint['runtime']!='codex':
            responses.append({'agent':agent,'error':'Native usage reader not connected'});continue
        try:
            result=request(config,endpoint,endpoint.get('prefix','')+'/api/usage')
            if not isinstance(result.get('native_threads'),list):raise ValueError('Missing reader')
            responses.append({'agent':agent,'data':result})
        except Exception:responses.append({'agent':agent,'error':'Usage report unavailable'})
    result={'at':time.time(),'responses':responses,'rooms':receipts,'desktop_rooms':len(rooms),
            'next_offset':(offset+len(selected))%max(1,len(supported)), 'model_calls':0,
            'coverage':'Observed Codex rooms from saved UX46 desktops; at most 24 refreshes per five-minute pass. Other runtimes are explicitly unmeasured.'}
    temp=root/'usage-collection.tmp';temp.write_text(json.dumps(result));temp.chmod(0o600);os.replace(temp,state_path)
    return {'at':result['at'],'rooms_refreshed':len(receipts),'rooms_indexed':sum(r['state']=='indexed' for r in receipts),'agents_reporting':sum('data' in r for r in responses),'model_calls':0}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--directory',required=True);args=p.parse_args()
    print(json.dumps(collect(json.loads(Path(args.config).read_text()),args.directory)))
