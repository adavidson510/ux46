#!/usr/bin/env python3
"""Incremental Gmail metadata checker; one process, bounded work, zero model calls.

Reads the existing owner-only OAuth files in place. Never sends, marks read,
labels, archives, creates drafts, or posts notifications. Results stay in UX46.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import stat
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from ux46_email import EmailStore

ACCOUNTS = {}  # Accounts are explicitly configured by the installation owner.

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Gmail:
    def __init__(self, token_file):
        p=Path(token_file)
        info=p.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError('OAuth file must be a regular owner-only file (0600)')
        # Values exist only inside this process and the intended Google request.
        creds=json.loads(p.read_text())
        if creds.get('token_uri','https://oauth2.googleapis.com/token')!='https://oauth2.googleapis.com/token':
            raise ValueError('Unexpected OAuth destination')
        body=urlencode({k:creds[k] for k in ('client_id','client_secret','refresh_token')} |
                       {'grant_type':'refresh_token'}).encode()
        request=Request('https://oauth2.googleapis.com/token',data=body,
                        headers={'Content-Type':'application/x-www-form-urlencoded'})
        with build_opener(NoRedirect).open(request,timeout=20) as response:
            self.token=json.load(response)['access_token']

    def get(self, resource, **query):
        url='https://gmail.googleapis.com/gmail/v1/users/me/'+resource
        if query:url+='?'+urlencode({k:v for k,v in query.items() if v is not None},doseq=True)
        req=Request(url,headers={'Authorization':'Bearer '+self.token})
        with build_opener(NoRedirect).open(req,timeout=20) as response:return json.load(response)


def metadata(raw,label_names):
    headers={h['name'].casefold():h.get('value','') for h in raw.get('payload',{}).get('headers',[])}
    labels=[label_names.get(x,x) for x in raw.get('labelIds',[])]
    warnings=[]
    if 'SPAM' in labels:warnings.append('Gmail flagged this message as spam')
    if any(x in headers.get('authentication-results','').casefold() for x in ('spf=fail','dkim=fail','dmarc=fail')):
        warnings.append('Sender authentication failure reported in message headers')
    return {'id':raw['id'],'thread':raw['threadId'],'stamp':int(raw.get('internalDate','0'))/1000,
            'subject':headers.get('subject','(No subject)'),'sender':headers.get('from',''),
            'recipient':headers.get('to',''),'delivered_to':headers.get('delivered-to',headers.get('x-original-to','')),
            'snippet':raw.get('snippet',''),'labels':labels,'warnings':warnings}


def check_account(store, aid, email, gmail, budget=100, deadline=None):
    """Advance a cursor only after every queued message is committed. Replays are safe."""
    deadline=deadline or time.monotonic()+90
    profile=gmail.get('profile')
    if profile.get('emailAddress','').casefold()!=email.casefold():raise ValueError('Authenticated Gmail account mismatch')
    label_names={l['id']:l['name'] for l in gmail.get('labels').get('labels',[])}
    state=store.sync_state(aid)
    if not state:
        state={'mode':'initial','cursor':profile['historyId'],'page':None,'pending':[],
               'query':'newer_than:30d OR (in:inbox label:"UX46/Needs You")',
               'coverage_from':time.time()-30*86400,'listing_done':False}
    processed=0
    while processed<budget and time.monotonic()<deadline:
        if state.get('pending'):
            mid=state['pending'][0]
            try:
                raw=gmail.get('messages/'+mid,format='metadata',metadataHeaders=
                              ['From','To','Delivered-To','X-Original-To','Subject','Date','Authentication-Results'])
                msg=metadata(raw,label_names)
                relevant=msg['stamp']>=state['coverage_from'] or ('INBOX' in msg['labels'] and any(x.startswith('UX46/') and 'Needed' in x or x=='UX46/Needs You' for x in msg['labels']))
                if relevant:store.upsert(aid,msg)
                else:store.delete_message(aid,mid)
            except HTTPError as exc:
                if exc.code!=404:raise
                store.delete_message(aid,mid)
            state['pending'].pop(0);processed+=1;store.sync_state(aid,state)
            continue
        if state.get('listing_done'):
            if state['mode']=='initial':
                state.update(mode='history',page=None,listing_done=False)
                store.sync_state(aid,state)
                # Follow the history from BEFORE the initial listing: no sync gap.
                continue
            state.update(cursor=state.get('next_cursor',state['cursor']),page=None,listing_done=False)
            state.pop('next_cursor',None)
            store.sync_state(aid,state)
            store.account(aid,email=email,status='healthy',last_success=time.time(),coverage_complete=True,
                          coverage_from=state['coverage_from'],checked_at=time.time(),processed=processed,error=None)
            return {'account':aid,'status':'healthy','processed':processed}
        if state['mode']=='initial':
            batch=gmail.get('messages',q=state['query'],maxResults=100,pageToken=state.get('page'),includeSpamTrash='true')
            ids=[m['id'] for m in batch.get('messages',[])]
        else:
            try:
                batch=gmail.get('history',startHistoryId=state['cursor'],maxResults=100,pageToken=state.get('page'))
            except HTTPError as exc:
                if exc.code!=404:raise
                # Expired history: reset bounded coverage, not an unbounded corpus scan.
                with store.db() as db:db.execute('DELETE FROM mail_messages WHERE account=?',(aid,))
                state.update(mode='initial',cursor=profile['historyId'],page=None,pending=[],listing_done=False,
                             query='newer_than:30d OR (in:inbox label:"UX46/Needs You")',coverage_from=time.time()-30*86400)
                store.sync_state(aid,state)
                store.account(aid,status='resyncing',coverage_complete=False)
                continue
            ids=sorted({m['id'] for h in batch.get('history',[]) for m in
                        (h.get('messages',[])+[v['message'] for key in ('messagesAdded','messagesDeleted','labelsAdded','labelsRemoved') for v in h.get(key,[])])})
            state['next_cursor']=batch.get('historyId',state['cursor'])
        state.update(pending=ids,page=batch.get('nextPageToken'),listing_done=not batch.get('nextPageToken'))
        store.sync_state(aid,state)
    store.account(aid,email=email,status='syncing',coverage_complete=False,checked_at=time.time(),processed=processed)
    return {'account':aid,'status':'syncing','processed':processed}


def run(db,account_root,budget=100,accounts=None):
    store=EmailStore(db)
    lock=Path(db).with_suffix('.checker.lock')
    with lock.open('a') as held:
        os.chmod(lock,0o600)
        try:fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'already-running','model_calls':0}
        results=[]
        for aid,email in (accounts or ACCOUNTS).items():
            store.account(aid,email=email,checked_at=time.time())
            try:
                gmail=Gmail(Path(account_root)/aid/'google_token.json')
                results.append(check_account(store,aid,email,gmail,budget))
            except Exception as exc:
                # Provider error bodies may contain sensitive request data; never log them.
                code='provider-'+str(exc.code) if isinstance(exc,HTTPError) else type(exc).__name__
                store.account(aid,status='error',error=code,coverage_complete=False,checked_at=time.time())
                results.append({'account':aid,'status':'error','error':code})
        return {'state':'checked','at':time.time(),'accounts':results,'model_calls':0,'provider_writes':0}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True)
    p.add_argument('--account-root',required=True)
    p.add_argument('--budget',type=int,default=100)
    p.add_argument('--accounts-config', type=Path, required=True, help='JSON mapping of account IDs to expected email addresses')
    p.add_argument('--account')
    args=p.parse_args()
    if not 1<=args.budget<=250:p.error('budget must be 1..250 messages/account/pass')
    accounts=json.loads(args.accounts_config.read_text())
    import re
    if not isinstance(accounts,dict) or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', k) or not isinstance(v,str) or '@' not in v for k,v in accounts.items()):p.error('Invalid accounts mapping')
    if args.account and args.account not in accounts:p.error('Unknown account')
    print(json.dumps(run(args.db,args.account_root,args.budget,
                         {args.account:accounts[args.account]} if args.account else accounts)))


if __name__=='__main__':main()
