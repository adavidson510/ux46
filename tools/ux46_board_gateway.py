"""Owner-authenticated boards, independent of native adapters and desktops."""
from http.client import HTTPConnection
import json
import re
import secrets
import sqlite3
from ux46_boards import BoardStore, Conflict


class BoardAPI:
    def __init__(self, path):
        self.store = BoardStore(path)

    def reply(self, handler, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        handler.send_response(status)
        handler._emit_login()
        handler.send_header('Content-Type','application/json; charset=utf-8')
        handler.send_header('Content-Length',str(len(data)))
        handler.send_header('Cache-Control','no-store')
        handler.send_header('X-Content-Type-Options','nosniff')
        if handler.close_connection: handler.send_header('Connection','close')
        handler.end_headers()
        if handler.command != 'HEAD': handler.wfile.write(data)

    def handle(self, handler):
        path = handler.path.split('?',1)[0]
        if not path.startswith('/api/boards/'): return False
        try:
            match = re.fullmatch(r'/api/boards/([A-Za-z0-9._-]{1,64})/([A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96})',path)
            if not match:
                handler._drain()
                self.reply(handler,404,{'error':'not_found','message':'No such board'})
                return True
            agent,room = match.groups()
            if handler.command in ('GET','HEAD'):
                self.reply(handler,200,self.store.read(agent,room)); return True
            if handler.command != 'PUT':
                handler._drain()
                self.reply(handler,405,{'error':'bad_method','message':'Use GET or PUT for a board'}); return True
            origin = handler.server.auth.origin_for(handler.headers.get('Host',''))
            if handler.headers.get('Origin','').rstrip('/').casefold() != origin.casefold():
                handler._drain()
                self.reply(handler,403,{'error':'bad_origin','message':'A change must come from this workspace.'}); return True
            upstream = handler.server.console
            connection = HTTPConnection(upstream.host, upstream.port, timeout=20)
            try:
                connection.request('GET','/api/bootstrap',headers={
                    'Host':handler.headers.get('Host',''),
                    handler.server.auth.identity_header:handler.headers.get(handler.server.auth.identity_header,'')})
                response = connection.getresponse()
                boot = json.loads(response.read())
                wanted = boot.get('csrf') if response.status == 200 else None
            finally: connection.close()
            if not isinstance(wanted,str) or not wanted or not secrets.compare_digest(wanted,handler.headers.get('X-Atlas-CSRF','')):
                handler._drain()
                self.reply(handler,403,{'error':'bad_csrf','message':'Refresh this page to reconnect.'}); return True
            length = int(handler.headers.get('Content-Length') or 0)
            if length < 1 or length > 24000:
                handler.close_connection = True
                self.reply(handler,413,{'error':'too_large','message':'Keep the board under 24 KB.'}); return True
            raw = handler.rfile.read(length)
            if len(raw) != length: raise ValueError('Incomplete board request')
            body = json.loads(raw)
            if not isinstance(body,dict): raise ValueError('Invalid board request')
            result = self.store.save(agent,room,body.get('base_version'),body.get('board'))
            self.reply(handler,200,result)
        except Conflict as exc:
            self.reply(handler,409,{'error':'board_conflict','message':str(exc),'detail':exc.current})
        except (ValueError, TypeError) as exc:
            handler.close_connection = True
            self.reply(handler,400,{'error':'bad_board','message':str(exc)})
        except (OSError, sqlite3.Error):
            handler.close_connection = True
            self.reply(handler,503,{'error':'board_unavailable','message':'The board could not be saved or read. Try again.'})
        return True
