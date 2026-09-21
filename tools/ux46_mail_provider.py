"""Gmail thread reads and explicit sends. OAuth stays in the existing owner file."""
import base64
import hashlib
import json
import re
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid
from html.parser import HTMLParser
from urllib.request import Request, build_opener
from ux46_email_check import Gmail, NoRedirect
from constellation_store import encoded

class PlainHTML(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag in ('br','p','div','li'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def addresses(value):
    if not isinstance(value,str) or any(c in value for c in '\r\n'):raise ValueError('Invalid recipients')
    found=getaddresses([value])
    if not found or len(found)>20 or any(not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',a) for _,a in found):
        raise ValueError('Use valid email addresses')
    return ', '.join(a for _,a in found)


def body_text(part):
    if part.get('filename'):return '',[part['filename']],False
    mime=part.get('mimeType','');body=part.get('body',{});data=body.get('data','')
    if mime in ('text/plain','text/html'):
        if body.get('attachmentId'):return '',[],True
        try:raw=base64.urlsafe_b64decode(data+'='*((-len(data))%4)).decode('utf-8',errors='replace')
        except (ValueError,TypeError):return '',[],True
        if mime=='text/html':
            h=PlainHTML();h.feed(raw);raw=''.join(h.parts)
        return raw,[],False
    children=[body_text(p) for p in part.get('parts',[])]
    if mime=='multipart/alternative':
        preferred=next((i for i,p in enumerate(part.get('parts',[])) if p.get('mimeType')=='text/plain'),0)
        children=children[preferred:preferred+1]
    return '\n'.join(x[0] for x in children),[n for x in children for n in x[1]],any(x[2] for x in children)


class MailProvider(Gmail):
    def __init__(self,token_file,expected_email):
        super().__init__(token_file)
        self.email=self.get('profile')['emailAddress']
        if self.email.casefold()!=expected_email.casefold():raise ValueError('Mail account does not match its configuration')

    def thread(self,ident):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident):raise ValueError('Invalid thread')
        raw=self.get('threads/'+ident,format='full');messages=[];complete=True
        if len(raw.get('messages',[]))>30:complete=False
        for m in raw.get('messages',[])[-30:]:
            p=m.get('payload',{});h={v['name'].lower():v.get('value','') for v in p.get('headers',[])}
            text,attachments,missing=body_text(p)
            if len(text)>14000 or missing:complete=False
            messages.append({'id':m['id'],'from':h.get('from',''),'to':h.get('to',''),'cc':h.get('cc',''),
                'reply_to':h.get('reply-to',h.get('from','')),'subject':h.get('subject',''),
                'date':h.get('date',''),'message_id':h.get('message-id',''),'references':h.get('references',''),
                'text':text[:14000],'attachments':attachments,'labels':m.get('labelIds',[])})
        if not messages:raise ValueError('Thread is empty')
        digest=hashlib.sha256(encoded([[m['id'],m['text'],m['attachments']] for m in messages])).hexdigest()
        return {'id':ident,'email':self.email,'messages':messages,'complete':complete,'revision':digest,
                'attachments_read':False}

    def send(self,draft):
        msg=EmailMessage();msg['From']=self.email;msg['To']=addresses(draft['to'])
        if draft.get('cc'):msg['Cc']=addresses(draft['cc'])
        msg['Subject']=draft['subject'];msg['Message-ID']=draft['message_id']
        if draft.get('in_reply_to'):msg['In-Reply-To']=draft['in_reply_to']
        if draft.get('references'):msg['References']=draft['references']
        msg.set_content(draft['body'])
        payload={'threadId':draft['thread'],'raw':base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        req=Request('https://gmail.googleapis.com/gmail/v1/users/me/messages/send',data=encoded(payload),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},method='POST')
        with build_opener(NoRedirect).open(req,timeout=30) as response:return json.load(response)

    def write(self,resource,payload):
        if resource not in ('labels','messages/batchModify'):raise ValueError('Unsupported mailbox operation')
        req=Request('https://gmail.googleapis.com/gmail/v1/users/me/'+resource,data=encoded(payload),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},method='POST')
        with build_opener(NoRedirect).open(req,timeout=30) as response:
            body=response.read();return json.loads(body) if body else {}

    def mailbox_snapshot(self,ident):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident):raise ValueError('Invalid thread')
        raw=self.get('threads/'+ident,format='minimal');messages=raw.get('messages',[])
        return {'complete':len(messages)<=100,'messages':[{'id':m['id'],'labels':m.get('labelIds',[])} for m in messages],
                'revision':hashlib.sha256(encoded([[m['id'],m.get('labelIds',[])] for m in messages])).hexdigest()}

    def label(self,name):
        if not re.fullmatch(r'UX46/[A-Za-z &-]{1,70}',name):raise ValueError('Invalid category label')
        if not hasattr(self,'label_cache'):self.label_cache={l['name']:l['id'] for l in self.get('labels').get('labels',[])}
        if name not in self.label_cache:self.label_cache[name]=self.write('labels',{'name':name,'labelListVisibility':'labelShow','messageListVisibility':'show'})['id']
        return self.label_cache[name]

    def modify(self,ids,add,remove):
        if not ids or len(ids)>100:raise ValueError('Invalid message batch')
        return self.write('messages/batchModify',{'ids':ids,'addLabelIds':add,'removeLabelIds':remove})
