"""Voice resolves UI-native IDs, not an incompatible full-history projection."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit, parse_qs
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from atlas_native import NativeSessions
from ux46_speech_lookup import find_message, SpeechLookupError
from ux46_voice_gateway import VoiceAPI
from test_ux46_access_gateway import GateCase, ORIGIN, USER, gate
from test_atlas_voice_speech import FakeKokoro


class NativeLookup(unittest.TestCase):
    def test_reads_exact_paged_id_without_using_stale_full_history(self):
        server = Mock()
        def request(method, args):
            self.assertEqual(method, 'thread/items/list')
            self.assertEqual(args['threadId'], 'our-thread')
            if args.get('cursor'):
                return {'data':[{'item':{'id':'reply','type':'agentMessage','text':'Actual native reply'}}], 'nextCursor':None}
            return {'data':[{'item':{'id':'newer','type':'commandExecution'}}], 'nextCursor':'page-2'}
        server.request.side_effect = request
        sessions = NativeSessions(server)
        sessions.full_history = Mock(side_effect=AssertionError('Wrong history source'))
        self.assertEqual(sessions.message_for_speech('our-thread','reply')['text'], 'Actual native reply')
        self.assertEqual(server.request.call_count, 2)

    def test_unavailable_and_repeating_pages_do_not_loop_or_invent_text(self):
        read = Mock(return_value={'items':[],'next_cursor':'same'})
        with self.assertRaises(SpeechLookupError):find_message(read,'missing')
        self.assertEqual(read.call_count,2)
        with self.assertRaises(SpeechLookupError):find_message(lambda _: {'items':[], 'unavailable':True},'missing')


class History(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        self.server.paths.append(self.path)
        u=urlsplit(self.path);q=parse_qs(u.query)
        if u.path=='/api/bootstrap': body={'csrf':'fixture-csrf'}
        elif u.path=='/api/room/project/room/history':
            if q.get('cursor')==['older']:
                body={'items':[{'id':'reply','type':'agentMessage','phase':'commentary','text':'The actual native response.'}],'complete':True}
            else:body={'items':[{'id':'tool','type':'commandExecution'}],'next_cursor':'older'}
        else:body={'items':[],'complete':True}
        raw=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


class SpeechGateway(GateCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.original_console=cls.console
        cls.console=ThreadingHTTPServer(('127.0.0.1',0),History);cls.console.paths=[];cls.console.seen=[]
        threading.Thread(target=cls.console.serve_forever,daemon=True).start()
        cls.server.console=gate.Upstream('http://127.0.0.1:'+str(cls.console.server_address[1]))
        cls.server.voice_api=VoiceAPI('/unused',Path(cls.tmp.name)/'speech')
        cls.fake=FakeKokoro();cls.server.voice_api.voice._kokoro=cls.fake
    @classmethod
    def tearDownClass(cls):
        cls.original_console.shutdown();cls.original_console.server_close();super().tearDownClass()
    def setUp(self):
        super().setUp();self.console.paths.clear();self.fake.heard.clear()
    def speak(self,body=None,headers=None,**kwargs):
        return self.ask('POST','/api/room/project/room/speak',body=json.dumps(body or {'item_id':'reply'}).encode(),
                        headers=headers if headers is not None else {'Origin':ORIGIN,'X-Atlas-CSRF':'fixture-csrf'},**kwargs)
    def test_live_reply_synthesizes_from_native_page_and_audio_supports_ranges(self):
        status,_,raw=self.speak();self.assertEqual(status,200,raw)
        speech=json.loads(raw);self.assertEqual(speech['item_id'],'reply')
        self.assertEqual(self.fake.heard,['The actual native response.'])
        self.assertTrue(any('cursor=older' in p for p in self.console.paths))
        status,headers,body=self.ask(path=speech['audio_url'],headers={'Range':'bytes=0-43'})
        self.assertEqual(status,206);self.assertEqual(len(body),44);self.assertTrue(body.startswith(b'RIFF'))
        self.assertEqual(self.ask(path=speech['audio_url'],password=None)[0],401)
    def test_identity_origin_csrf_and_browser_text_are_not_bypassed(self):
        for extra in [{'password':None},{'identity':'someone-else@example.com'}]:
            self.assertIn(self.speak(**extra)[0],(401,403))
        for headers in [{},{'Origin':ORIGIN},{'Origin':'https://elsewhere.test','X-Atlas-CSRF':'fixture-csrf'}]:
            self.assertEqual(self.speak(headers=headers)[0],403)
        self.assertEqual(self.speak({'item_id':'reply','text':'Invented browser text'})[0],400)
        self.assertEqual(self.fake.heard,[])
        self.assertFalse(any('/history' in p for p in self.console.paths))
    def test_missing_message_does_not_synthesize_another_reply(self):
        self.assertEqual(self.speak({'item_id':'other-room-id'})[0],404)
        self.assertEqual(self.fake.heard,[])
