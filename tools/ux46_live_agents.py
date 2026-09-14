"""Additional native adapters behind the authenticated gateway, reloadable alone.

Existing console routes retain their existing workers. This registry only adds
operator-configured agents; browser requests never choose a host or executable.
"""
import json
import re
import secrets
import threading
from http.client import HTTPConnection
from pathlib import Path
import atlas_remote as remote


class LiveAgents:
    def __init__(self, config_path):
        self.path = Path(config_path)
        self.lock = threading.RLock()
        self.stamp = None
        self.registry = None
        self.error = ''

    def read(self):
        with self.lock:
            try:
                stamp = self.path.stat().st_mtime_ns
                if stamp == self.stamp: return self.registry
                raw = json.loads(self.path.read_text())
                if raw.get('schema_version') != 1 or not isinstance(raw.get('agents'), list):
                    raise ValueError('invalid agent configuration')
                registry = remote.AgentRegistry(config_path=str(self.path), local_node='gateway')
                if registry.errors: raise ValueError('; '.join(registry.errors))
                # Transport replacement must never interrupt an in-flight request.
                # These are loopback additions; no child SSH transport is allowed.
                if any(not a.is_local and a.transport_kind != 'loopback' for a in registry.agents.values()):
                    raise ValueError('additional agents must use loopback adapters')
                self.registry, self.stamp, self.error = registry, stamp, ''
            except (OSError, ValueError, TypeError) as exc:
                self.error = str(exc)
            return self.registry

    def handle(self, handler):
        path, _, query = handler.path.partition('?')
        match = re.fullmatch(r'/api/agents/([a-z][a-z0-9-]{0,31})(/api/.*)', path)
        if path != '/api/agents' and not match: return False
        registry = self.read()
        if registry is None: return False
        if path == '/api/agents':
            if handler.command != 'GET': return False
            status, payload = self._console_json(handler, '/api/agents')
            if status != 200:
                self._reply(handler, status, payload); return True
            listed = payload.get('agents', [])
            ids = {a.get('id') for a in listed}
            for agent in registry.agents.values():
                if agent.is_local or agent.id in ids: continue
                agent.check()
                listed.append(agent.as_json())
            payload['agents'] = listed
            if self.error: payload.setdefault('errors', []).append('Additional agent configuration could not reload.')
            self._reply(handler, 200, payload); return True
        agent = registry.agents.get(match[1])
        if agent is None or agent.is_local: return False
        try:
            remote.check_allowed(handler.command, match[2], query)
            if handler.command not in ('GET', 'HEAD'):
                expected = handler.server.auth.origin_for(handler.headers.get('Host', ''))
                if handler.headers.get('Origin', '').rstrip('/').casefold() != expected.casefold():
                    self._reject(handler, 403, 'bad_origin', 'A change must come from this workspace.'); return True
                status, boot = self._console_json(handler, '/api/bootstrap')
                wanted = boot.get('csrf') if status == 200 else None
                if not isinstance(wanted, str) or not wanted or not secrets.compare_digest(wanted, handler.headers.get('X-Atlas-CSRF', '')):
                    self._reject(handler, 403, 'bad_csrf', 'Refresh this page to reconnect.'); return True
            length = int(handler.headers.get('Content-Length') or 0)
            if length > remote.MAX_PROXY_BODY:
                self._reject(handler, 413, 'too_large', 'That attachment is too large.'); return True
            body = handler.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                handler.close_connection = True
                self._reply(handler, 400, {'error':'short_body','message':'The request was incomplete.'});return True
            result = registry.proxy(agent, handler.command, match[2], query,
                headers={k:handler.headers.get(k,'') for k in remote.FORWARDED_REQUEST_HEADERS}, body=body)
            self._bytes(handler, result.status, result.body, result.content_type, result.headers)
        except remote.RemoteError as exc:
            handler.close_connection = True
            self._reply(handler, 502 if exc.code.startswith('agent_') else 400,
                        {'error':exc.code, 'message':str(exc)})
        except (OSError, ValueError):
            handler.close_connection = True
            self._reply(handler, 502, {'error':'agent_unavailable','message':'The agent connection did not answer. Nothing was resent.'})
        return True

    def _console_json(self, handler, path):
        upstream = handler.server.console
        connection = HTTPConnection(upstream.host, upstream.port, timeout=20)
        try:
            # Let the original console enforce its own exact trusted identity.
            headers = {'Host':handler.headers.get('Host',''), 'Accept':'application/json',
                       handler.server.auth.identity_header:handler.headers.get(handler.server.auth.identity_header,'')}
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body)
        finally: connection.close()

    def _reject(self, handler, status, code, message):
        handler.close_connection = True
        self._reply(handler, status, {'error':code,'message':message})

    def _reply(self, handler, status, payload):
        self._bytes(handler, status, json.dumps(payload).encode(), 'application/json; charset=utf-8')

    def _bytes(self, handler, status, body, content_type, headers=()):
        handler.send_response(status); handler._emit_login()
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control','no-store')
        handler.send_header('X-Content-Type-Options','nosniff')
        for key,value in headers: handler.send_header(key,value)
        if handler.close_connection:handler.send_header('Connection','close')
        handler.end_headers()
        if handler.command != 'HEAD': handler.wfile.write(body)
