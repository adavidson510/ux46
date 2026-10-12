"""Bounded room reads and verified model/effort changes for the owner's assistant.

No inference for reads, no source transcript forwarding, no arbitrary commands.
Settings are desired state, read back from the native runtime. A partial change
or busy runtime is reported as such; this helper never invents a queued job.
"""
import argparse
import json
import time
from urllib.parse import quote
from ux46_assistant_handoffs import resolve, current_human, local_clients


def detail_for(dst, clients):
    code,detail=clients[dst['agent']].request('GET','/api/room/'+dst['room']+'?inspect=1')
    if code!=200 or detail.get('native',{}).get('thread_id')!=dst['thread']:
        raise ValueError('Room identity changed or is unavailable; resolve it again')
    return detail


def native_status(dst, clients):
    detail_for(dst,clients)
    code,result=clients[dst['agent']].request('POST','/api/room/'+dst['room']+'/command',
        {'command':'/status','thread_id':dst['thread']})
    command=result.get('command',{})
    if code!=200 or command.get('state')!='ready':raise ValueError('Native settings could not be checked')
    detail_for(dst,clients)
    return command


def read(cfg, room, clients, query=''):
    dst=resolve(cfg,room,clients,allow_self=True);detail=detail_for(dst,clients)
    native=detail.get('native',{})
    # Checkpoints are dated reports, not proof of a currently working agent.
    result={'room':dst,'checked_at':time.time(),'runtime':detail.get('runtime'),
        'model':native.get('model'),'reasoning_effort':native.get('reasoning_effort'),
        'active_turn':native.get('active_turn'),'checkpoint':detail.get('checkpoint'),
        'attention':detail.get('attention'),'reported_next':detail.get('next_action')}
    path=('/search?q='+quote(query,safe='')+'&kinds=human,final&limit=8' if query
          else '/history?limit=24&direction=desc')
    code,history=clients[dst['agent']].request('GET','/api/room/'+dst['room']+path)
    unavailable=code!=200 or history.get('unavailable') or history.get('source')=='unavailable'
    result['history_available']=not bool(unavailable)
    rows=[] if unavailable else history.get('hits' if query else 'items',[])
    selected=[];budget=12000
    for item in rows:
        if not query and item.get('type') not in ('userMessage','agentMessage'):continue
        if item.get('type')=='agentMessage' and item.get('phase') not in ('commentary','final_answer'):continue
        text=item.get('text',item.get('snippet',''))
        if not isinstance(text,str):continue
        text=text[:min(2500,budget)];budget-=len(text)
        selected.append({k:item[k] for k in ('id','turn_id','type','phase','timestamp','created_at') if k in item}|{'text':text})
        if budget<=0 or len(selected)>=8:break
    result.update(messages=selected,history_scope='bounded search' if query else 'recent messages only',
                  note='Checkpoints and replies are reports. Check their dates; unavailable history is not an empty room.')
    detail_for(dst,clients)
    return result


def settings(cfg, room, clients, *, request='', model=None, effort=None):
    dst=resolve(cfg,room,clients,allow_self=True)
    before=native_status(dst,clients)
    if not model and not effort:
        return {'room':dst,'checked_at':time.time(),**before}
    current_human(cfg,request,clients)
    wanted_model=model or before.get('native',{}).get('model')
    catalog={x.get('model',x.get('id')):x for x in before.get('catalog',{}).get('models',[])}
    if wanted_model not in catalog:raise ValueError('Choose a model advertised by this room')
    if effort and effort not in catalog[wanted_model].get('efforts',[]):raise ValueError('Choose a reasoning effort advertised for that model')
    client=clients[dst['agent']];steps=[];refreshed=False
    for label,value in (('model',model),('reasoning',effort)):
        if not value:continue
        current_human(cfg,request,clients);detail_for(dst,clients)
        body={'command':'/'+label+' '+value,'thread_id':dst['thread']}
        try:
            code,result=client.request('POST','/api/room/'+dst['room']+'/command',body)
            # Only a definite unsupported-model refusal may refresh/retry once.
            # A timeout is unknown, never permission to replay a command.
            error=result.get('error',{})
            error_code=error.get('code') if isinstance(error,dict) else (error or result.get('error_code'))
            if label=='model' and error_code=='unsupported_model' and not refreshed:
                current_human(cfg,request,clients);detail_for(dst,clients)
                rc,refresh=client.request('POST','/api/connection/refresh',{'room':dst['room']})
                connection=refresh.get('connection',refresh)
                if rc==200 and connection.get('state')=='refreshed' and connection.get('scope')=='conversation' and connection.get('room')==dst['room']:
                    refreshed=True;detail_for(dst,clients)
                    code,result=client.request('POST','/api/room/'+dst['room']+'/command',body)
            command=result.get('command',{})
            steps.append({'setting':label,'state':command.get('state') or ('refused' if code<500 else 'unknown'),
                          'error_code':error_code})
            if code!=200 or command.get('state')!='updated':break
        except Exception:
            steps.append({'setting':label,'state':'unknown'});break
    try:
        after=native_status(dst,clients)['native']
        verified=(not model or after.get('model')==model) and (not effort or after.get('reasoning_effort')==effort)
    except Exception:after=None;verified=False
    return {'room':dst,'checked_at':time.time(),'state':'verified' if verified else 'not_verified',
        'requested':{'model':model,'reasoning_effort':effort},'before':before.get('native'),
        'after':after,'steps':steps,'connection_refreshed':refreshed,
        'note':'Settings apply to later turns. This does not change an already running turn. No retry job has been queued.'}


def main():
    from ux46_concierge import Concierge
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',required=True);p.add_argument('action',choices=['read','settings'])
    p.add_argument('--room',required=True);p.add_argument('--query',default='')
    p.add_argument('--request',default='');p.add_argument('--model');p.add_argument('--effort')
    a=p.parse_args()
    try:
        cfg=Concierge(a.directory).settings();clients=local_clients(a.directory,cfg)
        result=(read(cfg,a.room,clients,a.query) if a.action=='read' else
                settings(cfg,a.room,clients,request=a.request,model=a.model,effort=a.effort))
        print(json.dumps(result,ensure_ascii=False));return 0
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ValueError) else 'Room operation unavailable; check its current settings before retrying'}));return 1


if __name__=='__main__':raise SystemExit(main())
