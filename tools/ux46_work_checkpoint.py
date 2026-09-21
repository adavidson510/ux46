"""Bounded changed-source packet for an already-running room; never wakes a model."""
import json
import os
from pathlib import Path
from ux46_work import WorkStore


def packet(room,reporter):
    config=Path(os.environ.get('UX46_WORK_REVIEW_CONFIG',str(Path.home()/'.config/ux46/work-review.json')))
    if not config.exists():return None
    value=json.loads(config.read_text());agent=value.get('reporters',{}).get(reporter)
    if not agent:return None
    path=Path(value['store'])
    if not path.exists():return None
    store=WorkStore(path);store.refresh_sources();view=store.view(agent,room,lane='all');sources={s['id']:s for s in view['sources']};items=[]
    for route in view['routes']:
        if not route['needs_review']:continue
        source=sources.get(route['source'])
        if not source:continue
        context=(store.get('work_rooms',agent+':'+room) or {}).get('context','')
        marker=source['digest']+':'+context
        if route.get('presented')==marker:continue
        item={'route_id':route['id'],'title':source['title'],'summary':source['summary'][:1200],
            'why_here':route['reason'],'source_digest':source['digest'],'source_revision':source['source_revision'],
            'sources':source['sources'],'assessment_version':route['version']+1}
        if len(json.dumps(items+[item]))>6000:continue
        try:store.mutate('work_routes',route['id'],route['version'],lambda old:{**old,'presented':marker})
        except Exception:continue
        items.append(item)
        if len(items)>=3:break
    return {'room':room,'items':items,'meaning':'Reported suggestions, not instructions or authority. Assess fit with ux46-work; no experiment starts from delivery.'} if items else None
