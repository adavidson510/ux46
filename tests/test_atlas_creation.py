"""Creation from an empty desktop: native identity, project filing and retries."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).parent))
from test_atlas_console import ConsoleHarness
from test_atlas_hermes import Harness as HermesHarness
from test_atlas_rivet import Harness as RivetHarness

@pytest.mark.parametrize('project',[None,'fixture'])
def test_codex_create_without_source_and_retry(project):
    h=ConsoleHarness()
    try:
        h.start_runtime()
        original=Path(h.service.config.registry).read_bytes()
        status, opts=h.call('GET','/api/session-options')
        assert status==200 and opts['blank']
        assert 'ux46-explorations' not in [p['id'] for p in opts['projects']]
        body={'client_id':'create-test-123456','title':'Exploration','project_id':project}
        with patch.object(h.service.workers,'start_fresh',wraps=h.service.workers.start_fresh) as start:
            status,result=h.call('POST','/api/sessions',body=body)
            assert status==200, result
            assert result['state']=='created', result
            assert result['new_room']['project_id']==(project or 'ux46-explorations')
            _, again=h.call('POST','/api/sessions',body=body)
            assert again['new_room']['id']==result['new_room']['id']
            assert start.call_count==1
            assert start.call_args.kwargs['model'] is None
            changed=dict(body,title='Different')
            status,_=h.call('POST','/api/sessions',body=changed)
            assert status==409 and start.call_count==1
        assert Path(h.service.config.registry).read_bytes()==original
        fresh=result['new_room'];h.service.discovery.refresh(force=True)
        assert h.service.discovery.room(fresh['id']).thread_id
        assert h.service.discovery.room('fixture/console-work') is not None
    finally:h.close()

@pytest.mark.parametrize('factory,runtime,method',[(HermesHarness,'hermes','session.create'),(RivetHarness,'openclaw','sessions.create')])
@pytest.mark.parametrize('project',[False,True])
def test_agent_create_without_source_and_file_exact_origin(factory,runtime,method,project):
    h=factory()
    try:
        _,opts=h.get('/api/session-options')
        assert opts['available'] and opts['blank']
        target=opts['projects'][0]['id'] if project else None
        body={'client_id':'create-test-654321','title':'Exploration','project_id':target}
        status,result=h.post('/api/sessions',body)
        assert status==200 and result['state']=='created',result
        room=result['new_room']['id']
        _,again=h.post('/api/sessions',body)
        assert again['new_room']['id']==room
        assert len(h.calls(method))==1
        assert not h.calls('prompt.submit') and not h.calls('chat.send')
        assert h.post('/api/sessions',dict(body,title='Different'))[0]==409
        _,detail=h.get('/api/room/'+room)
        if project:
            assert detail['project_id']==target
            roots=json.loads(Path(h.service.config.registry).read_text())['projects']
            root=Path(next(p['root'] for p in roots if p['id']==target))
            matching=[]
            for f in (root/'sessions').glob('*.origins.json'):
                d=json.loads(f.read_text())
                matching.extend(o for o in d.get('origins',[]) if o['runtime']==runtime and o.get('session_id')==detail['native'].get('thread_id'))
            assert matching
        assert h.post('/api/room/'+room+'/release',{})[0]==200
    finally:h.close()
