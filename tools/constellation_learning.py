"""Small situation briefs from sourced records and reported outcomes. No models."""
import math
import re
import time
import unicodedata
from collections import Counter
from constellation_store import encoded, text, identifier

STOP = set('a an the is are was were be been being it its this that these those i me my you your we our they their to from for of on in at with and or as by can could would should do does did have has had how what when where why please need want use using work working something about into'.split())


def words(value):
    tokens = re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold())
    # Whole words and conservative plural folding, never substring matches such as ai in failure.
    return {t[:-1] if len(t)>4 and t.endswith('s') and not t.endswith(('ss','us','is')) else t
            for t in tokens if t not in STOP and len(t)>1}


def outcomes(record, feedback):
    reports=[f for f in feedback if f['lesson_id']==record['id']]
    current=[f for f in reports if f['lesson_revision']==record['revision']]
    verdicts=Counter(f['verdict'] for f in current)
    state='mixed-reports' if verdicts['helped'] and verdicts['failed'] else 'reported-failure' if verdicts['failed'] else 'reported-helpful' if verdicts['helped'] else 'applied-unresolved' if verdicts['used'] else 'not-yet-applied'
    applied=[f for f in current if f['verdict'] in ('helped','failed','used')]
    people={f['principal'] for f in applied}
    # One reporter cannot dominate ranking by repeating votes across many tasks.
    votes=[sum(1 if f['verdict']=='helped' else -1 for f in current if f['principal']==p and f['verdict'] in ('helped','failed')) /
           max(1,sum(f['principal']==p and f['verdict'] in ('helped','failed') for f in current)) for p in people]
    adjustment=round(.15*sum(votes)/(len(votes)+2),4)
    failing={f['principal'] for f in current if f['verdict']=='failed'}
    held=record['state']=='contradicted' or (len(failing)>=2 and verdicts['failed']>verdicts['helped'])
    return {'state':state,'helped':verdicts['helped'],'failed':verdicts['failed'],
            'used':verdicts['used'],'reporters':len({f['principal'] for f in current}),
            'applications':len(applied),'not_applicable':verdicts['not-applicable'],
            'ranking_adjustment':adjustment,'held_for_review':held,
            'earlier_revision_reports':len(reports)-len(current),
            'reports':[{'id':f['id'],'by':f['principal'],'verdict':f['verdict'],'reason':f['reason'],'evidence':f.get('evidence','')}
                       for f in sorted(current,key=lambda f:(f['verdict']=='failed',f['at']),reverse=True)
                       if f['verdict'] in ('helped','failed','not-applicable')][:2],
            'corrections':[{'by':f['principal'],'suggestion':f['suggestion'],'evidence':f.get('evidence','')}
                           for f in current if f.get('suggestion')][-2:]}


def warnings(record, outcome, records, now):
    result=[]
    if record['state']=='contradicted':result.append('Record is marked contradicted; inspect before applying.')
    if record['state']=='proposed' or record['evidence'] in ('inference','proposal'):
        result.append('Unvalidated proposal or inference; check fit.')
    if record.get('review_after') and now>=record['review_after']:
        result.append('The author’s review date has passed; verify current applicability.')
    if outcome['failed']:result.append('Failure reported for this revision; inspect the conditions and feedback.')
    for other in records:
        if other['state'] in ('retired','superseded'):continue
        if any(link['target']==record['id'] and link['type']=='contradicts' and link['state'] not in ('retired','superseded') for link in other.get('links',[])):
            result.append('A connected record disputes this lesson: '+other['id'])
    return result[:4]


def rank(records, feedback, query, project='', include_flagged=False):
    tokens=words(query)
    active=[r for r in records if r['state'] not in ('retired','superseded')]
    fields={r['id']:[words(' '.join(r.get('terms',[])+r['projects']+r.get('subjects',[]))),
                       words(r['claim']),words(r['rationale']+' '+r['applies']+' '+r['limits']+' '+' '.join(r.get('learning',{}).values()))] for r in active}
    frequency=Counter(t for values in fields.values() for t in set().union(*values))
    ranked=[]
    for r in active:
        bags=fields[r['id']];matched=sorted(tokens & set().union(*bags))
        if tokens and not matched:continue
        outcome=outcomes(r,feedback)
        if outcome['held_for_review'] and not include_flagged:continue
        score=sum((1+math.log((1+len(active))/(1+frequency[t])))*sum(w for w,bag in zip((4,3,1),bags) if t in bag) for t in matched)
        score+=min(2,len(matched)) if project and project in r['projects'] else 0
        # Relevance dominates; outcomes only make a bounded adjustment, never a popularity score.
        score=(score or 1)*(1+outcome['ranking_adjustment'])
        ranked.append((round(score,5),r,matched,None))
    ranked.sort(key=lambda x:(-x[0],x[1]['id']))
    return ranked


def brief(records, feedback, query, project='', limit=3, budget_bytes=6000, include_flagged=False):
    query=text(query,500,'situation');project=text(project,96,'project',True)
    if type(limit) is not int or not 1<=limit<=5:raise ValueError('Use one to five lessons')
    if type(budget_bytes) is not int or not 2000<=budget_bytes<=12000:raise ValueError('Brief budget must be 2000–12000 bytes')
    if type(include_flagged) is not bool:raise ValueError('include_flagged must be boolean')
    if not words(query):raise ValueError('Describe the decision or situation')
    ranked=rank(records,feedback,query,project,include_flagged)
    active=[r for r in records if r['state'] not in ('retired','superseded') and (include_flagged or not outcomes(r,feedback)['held_for_review'])]
    seeds=ranked[:min(2,limit)]
    direct_ids={r[1]['id'] for r in ranked};neighbors=[]
    # One hop, including reverse links. No recursive expansion or hidden query into another scope.
    for score,seed,matched,_ in seeds:
        for r in active:
            if r['id'] in direct_ids:continue
            links=[(l,seed['id'],r['id']) for l in seed.get('links',[]) if l['target']==r['id']]
            links += [(l,r['id'],seed['id']) for l in r.get('links',[]) if l['target']==seed['id']]
            for link,source,target in links:
                if link['state'] in ('retired','superseded'):continue
                neighbors.append((score*.35,r,[],{'from':source,'to':target,'type':link['type'],'state':link['state'],'reason':link['reason']}))
    neighbors.sort(key=lambda x:(-x[0],x[1]['id']))
    chosen=ranked[:min(2,limit)]
    for candidate in sorted(neighbors+ranked[min(2,limit):],key=lambda x:(-x[0],x[1]['id'])):
        if len(chosen)>=limit:break
        if candidate[1]['id'] not in {x[1]['id'] for x in chosen}:chosen.append(candidate)
    now=time.time();items=[]
    for score,r,matched,via in chosen:
        outcome=outcomes(r,feedback)
        # Exact get keeps complete feedback detail; recall only needs enough to judge fit.
        outcome['reports']=[dict(f,reason=f['reason'][:300],evidence=f['evidence'][:200]) for f in outcome['reports']]
        item={k:r[k] for k in ('id','revision','claim','kind','evidence','state','owner','classification','projects','applies','limits')}
        item.update(origin=r.get('origin','unspecified'),learning=r.get('learning',{}),subjects=r.get('subjects',[]),
                    match={'terms':matched,'via':via,'project_match':bool(project and project in r['projects'])},
                    outcome=outcome,warnings=warnings(r,outcome,records,now),
                    sources=[{k:s[k] for k in ('id','room','revision')} for s in r['sources']])
        items.append(item)
    result={'items':items,'coverage':'authorized captured lessons; project is a relevance hint, not a scope filter',
            'scope':'No inference of permission from shared knowledge','offline':False,
            'ranking':'word relevance with capped reported-use adjustment; two reporters with predominant failures hold a revision for review',
            'measurement':{'returned_bytes':0,'budget_bytes':budget_bytes,'candidates':len(ranked),'truncated':False,'model_calls':0}}
    # Bound the complete response, including measurement and caveats, not just the list.
    while True:
        result['measurement']['returned_bytes']=len(encoded(result))
        if len(encoded(result))<=budget_bytes:break
        if not items:raise ValueError('Brief cannot fit the byte budget')
        items.pop();result['measurement']['truncated']=True
    for _ in range(3):result['measurement']['returned_bytes']=len(encoded(result))
    return result


def catalog(records, after='', limit=20, subject='', project=''):
    if after:identifier(after)
    if type(limit) is not int or not 1<=limit<=20:raise ValueError('Catalog page is one to twenty entries')
    subject=text(subject,80,'subject',True);project=text(project,96,'project',True)
    active=sorted((r for r in records if r['state'] not in ('retired','superseded') and
                   (not project or project in r['projects']) and
                   (not subject or words(subject)<=words(' '.join(r.get('subjects',[])+[r['kind']])))),key=lambda r:r['id'])
    page=[r for r in active if r['id']>after][:limit+1];more=len(page)>limit;page=page[:limit]
    return {'items':[{k:r[k] for k in ('id','revision','claim','kind','origin','projects','subjects') if k in r} for r in page],
            'after':page[-1]['id'] if page else after,'more':more,'coverage':'authorized active lessons only'}


def review(records, feedback, observations=()):
    active=[r for r in records if r['state'] not in ('retired','superseded')];items=[]
    for r in active:
        outcome=outcomes(r,feedback);flags=warnings(r,outcome,active,time.time())
        shown={(m['principal'],int(m['at']//86400)) for m in observations if
               {'id':r['id'],'revision':r['revision']} in m.get('lessons',[])}
        age_days=int((time.time()-r['updated_at'])/86400)
        if outcome['held_for_review']:flags.insert(0,'Held out of normal recall pending correction or review; exact lookup remains available.')
        if outcome['not_applicable']>=3:flags.append('Repeatedly reported as not applicable: review retrieval terms and conditions, not just the claim.')
        if not outcome['applications'] and ((len(shown)>=5 and age_days>=30) or age_days>=90):
            flags.append('No reported application: review discoverability and rarity before archiving. Absence of feedback is not failure.')
        if flags or outcome['corrections']:
            items.append({'id':r['id'],'revision':r['revision'],'claim':r['claim'],'owner':r['owner'],
                          'warnings':flags,'outcome':outcome,'shown_days_in_window':len(shown),'age_days':age_days})
    items.sort(key=lambda x:(not bool(x['outcome']['failed'] or x['outcome']['corrections']),x['id']))
    selected=items[:10]
    while len(encoded(selected))>10000:selected.pop()
    return {'items':selected,'more':len(selected)<len(items),'coverage':'bounded review candidates, not automatic retirement; exposure covers latest 500 observations'}
