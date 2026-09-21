"""Human-only workspace API behind the existing authenticated UX46 gateway."""
import json
import secrets
import sqlite3
import time
from http.client import HTTPConnection
from urllib.parse import urlsplit, parse_qs
from constellation_store import Store, Principal, Conflict, Unavailable
from ux46_email import EmailStore
from ux46_schedule import ScheduleStore
from ux46_board_gateway import BoardAPI


def constellation_call(store, principal, operation, args):
    if operation in ('brief','catalog','review'):return store.learning_view(principal,operation,args)
    if operation=='lookup':return store.lookup(principal,**{k:args[k] for k in ('query','project','kinds','limit') if k in args})
    if operation=='get':return store.get(principal,args['id'],int(args['revision']) if 'revision' in args else None)
    if operation=='source':return store.source(principal,args['refs'])
    if operation=='capture':return store.capture(principal,args)
    if operation=='feedback':return store.feedback(principal,args)
    if operation=='changes':return store.changes(principal,args.get('cursor',''),args.get('limit',20))
    if operation=='health':return store.health(principal)
    raise Unavailable('Unknown operation')


class WorkspaceAPI(BoardAPI):
    def __init__(self,directory,constellation_config=None,modules=None):
        from pathlib import Path
        self.modules = modules if modules is not None else {'constellation': True, 'email': True}
        self.constellation=None if constellation_config or not self.modules.get('constellation') else Store(Path(directory)/'constellation.sqlite3')
        self.email=EmailStore(Path(directory)/'email.sqlite3') if self.modules.get('email') else None
        from ux46_work import WorkStore
        self.work_refresh=0
        self.work=WorkStore(Path(directory)/'work.sqlite3')
        self.schedule=ScheduleStore(directory)
        from ux46_mail_assistant import MailAssistant
        self.mail_assistant=MailAssistant(directory) if self.email else None
        from ux46_devices import DeviceStore
        self.devices=DeviceStore(Path(directory)/'devices.sqlite3')
        from ux46_agent_actions import RefreshJobs
        self.agent_jobs=RefreshJobs(Path(directory)/'agent-refresh.sqlite3')
        self.usage_report_path=Path(directory)/'usage-collection.json'
        self.owner=Principal('user',('*',),True)
        self.constellation_client=None
        if constellation_config:
            from constellation_relay import Client
            self.constellation_client=Client(json.loads(Path(constellation_config).read_text()))

    def dispatch(self,path,args):
        if path=='/api/desktop-devices/view':return self.devices.view(args['browser'])
        if path=='/api/desktop-devices/check-in':return self.devices.check_in(args)
        if path=='/api/desktop-devices/rename':return self.devices.rename(args)
        if path=='/api/desktop-devices/associate':return self.devices.associate(args)
        if path.startswith('/api/email/') and not self.modules.get('email'):raise Unavailable('Email disabled')
        if path.startswith('/api/constellation/'):
            if not self.modules.get('constellation'):raise Unavailable('Constellation disabled')
            if self.constellation_client:
                from constellation_relay import Refused
                try:return self.constellation_client.request(path.rsplit('/',1)[-1],args)['result']
                except Refused as exc:
                    if exc.status==409:raise Conflict('Record changed; refresh before saving')
                    if exc.status in (401,403):raise PermissionError('Shared learning access refused')
                    if exc.status==404:raise Unavailable('Unavailable in shared learning')
                    raise OSError('Shared learning unavailable')
            return constellation_call(self.constellation,self.owner,path.rsplit('/',1)[-1],args)
        if path=='/api/usage-report/view':
            if not self.usage_report_path.exists():return {'responses':[], 'coverage':'Usage collection has not run yet.'}
            if self.usage_report_path.stat().st_size>8*1024*1024:raise ValueError('Usage report is too large')
            return json.loads(self.usage_report_path.read_text())
        if path=='/api/work/view':
            if time.monotonic()-self.work_refresh>60:
                self.work_refresh=time.monotonic()
                self.work.refresh_sources(lambda ident:self.dispatch('/api/constellation/get',{'id':ident}))
            return self.work.view(**{k:args[k] for k in ('agent','room','lane') if k in args})
        if path=='/api/work/action':
            result=self.work.action(args)
            if args.get('action')=='outcome':
                source=self.work.get('work_sources',result['source'])
                if source and source['kind']=='constellation' and source.get('lesson_id'):
                    payload={'key':'experiment-'+result['id']+'-'+str(result['version']),'id':source['lesson_id'],
                        'revision':int(result['source_revision']),'use_id':'experiment-'+result['id'],'base_revision':result.get('learning_feedback_revision',0),
                        'verdict':result['outcome'],'reason':result['reason'][:600],'evidence':result['evidence'][:600]}
                    try:
                        receipt=self.dispatch('/api/constellation/feedback',payload)
                        result=self.work.mutate('work_experiments',result['id'],result['version'],lambda old:{**old,'learning_feedback':'recorded','learning_feedback_revision':receipt['revision']})
                    except (ValueError,OSError,PermissionError):result['learning_feedback']='pending; outcome saved locally'
            return result
        if path=='/api/schedule/view':return self.schedule.view()
        if path=='/api/schedule/action':return self.schedule.action(args)
        if path=='/api/email/assistant':return self.mail_assistant.status()
        if path=='/api/email/assistant-action':return self.mail_assistant.action(args)
        if path=='/api/email/thread':return self.mail_assistant.provider(args['account']).thread(args['thread'])
        if path=='/api/email/view':return self.email.view(**{k:args[k] for k in ('account','category','lane','project') if k in args})
        if path=='/api/email/status':
            view=self.email.view()
            return {k:view[k] for k in ('counts','accounts','coverage_complete')}
        if path=='/api/email/attention':return self.email.attention(args)
        if path=='/api/email/rule':return self.email.save_rule(args)
        raise Unavailable('Unknown workspace operation')

    def handle(self,handler):
        path=urlsplit(handler.path).path
        if not path.startswith(('/api/agent-actions/','/api/desktop-devices/','/api/constellation/','/api/email/','/api/schedule/','/api/usage-report/','/api/work/')):return False
        try:
            allowed_get={'/api/work/view','/api/agent-actions/view','/api/desktop-devices/view','/api/constellation/catalog','/api/constellation/review','/api/usage-report/view','/api/schedule/view','/api/constellation/lookup','/api/constellation/get','/api/constellation/health',
                         '/api/constellation/changes','/api/email/view','/api/email/status','/api/email/assistant'}
            if handler.command in ('GET','HEAD'):
                if path not in allowed_get:raise ValueError('Use POST for this operation')
                args={k:v[-1] for k,v in parse_qs(urlsplit(handler.path).query).items()}
            elif handler.command=='POST':
                origin=handler.server.auth.origin_for(handler.headers.get('Host',''))
                if handler.headers.get('Origin','').rstrip('/').casefold()!=origin.casefold():
                    raise PermissionError('A change must come from this workspace')
                upstream=handler.server.console
                connection=HTTPConnection(upstream.host,upstream.port,timeout=10)
                try:
                    connection.request('GET','/api/bootstrap',headers={'Host':handler.headers.get('Host',''),
                        handler.server.auth.identity_header:handler.headers.get(handler.server.auth.identity_header,'')})
                    response=connection.getresponse();boot=json.loads(response.read(200000))
                    wanted=boot.get('csrf') if response.status==200 else None
                finally:connection.close()
                if not isinstance(wanted,str) or not wanted or not secrets.compare_digest(wanted,handler.headers.get('X-Atlas-CSRF','')):
                    raise PermissionError('Refresh the workspace before saving')
                length=int(handler.headers.get('Content-Length','0'))
                if not 0<length<=16000:raise ValueError('Request must be under 16 KB')
                args=json.loads(handler.rfile.read(length))
                if not isinstance(args,dict):raise ValueError('Expected an object')
            else:raise ValueError('Unsupported method')
            if path=='/api/agent-actions/view':result=self.agent_jobs.view(args['agent'])
            elif path=='/api/agent-actions/refresh':
                from ux46_agent_actions import AgentClient
                result=self.agent_jobs.start(args['agent'],args['client_id'],AgentClient(handler,args['agent']))
            else:result=self.dispatch(path,args)
            self.reply(handler,200,result)
        except PermissionError as exc:self.reply(handler,403,{'error':'forbidden','message':str(exc)})
        except Conflict as exc:self.reply(handler,409,{'error':'conflict','message':str(exc)})
        except Unavailable as exc:self.reply(handler,404,{'error':'unavailable','message':str(exc)})
        except (ValueError,TypeError,KeyError):self.reply(handler,400,{'error':'invalid_request','message':'Check the requested fields and refresh before retrying.'})
        except (OSError,sqlite3.Error):self.reply(handler,503,{'error':'unavailable','message':'Workspace store unavailable. Your last saved state is preserved.'})
        handler.close_connection=True
        return True
