"""Optional local speech at the authenticated front door, independent of workers.

Only native message IDs are accepted. Text is fetched from the configured
agent's paged history; clients cannot submit arbitrary text or destinations.
"""
import json
import re
import secrets
from urllib.parse import urlencode
from atlas_voice import VoiceService, VoiceError, DEFAULT_VOICE, audio_response
from atlas_remote import RemoteError
from ux46_live_agents import LiveAgents
from ux46_speech_lookup import find_message, SpeechLookupError
from atlas_native import _item_text


class VoiceAPI:
    def __init__(self, model_dir, cache_dir):
        self.voice = VoiceService(model_dir, cache_dir)
        # Reuse authenticated fixed-console reads and byte response framing.
        self.io = LiveAgents.__new__(LiveAgents)

    def read(self, handler, prefix, path):
        extra = getattr(handler.server, 'live_agents', None)
        if prefix and extra:
            registry = extra.read()
            agent = registry.agents.get(prefix.split('/')[3]) if registry else None
            if agent and not agent.is_local:
                suffix, _, query = path.partition('?')
                result = registry.proxy(agent, 'GET', suffix, query, headers={}, body=None)
                if result.status != 200:
                    raise SpeechLookupError('The agent history could not be read.', 'history_unavailable')
                return json.loads(result.body)
        status, payload = self.io._console_json(handler, prefix + path)
        if status != 200:
            raise SpeechLookupError('The conversation could not be read.', 'history_unavailable')
        return payload

    def handle(self, handler):
        path = handler.path.split('?', 1)[0]
        prefix = r'(/api/agents/[a-z][a-z0-9-]{0,31})?'
        match = re.fullmatch(prefix + r'/api/room/([A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96})/speak', path)
        audio = re.fullmatch(prefix + r'/api/audio/([0-9a-f]{32,64})\.wav', path)
        if audio and handler.command in ('GET', 'HEAD'):
            clip = self.voice.path_for(audio[2])
            if not clip.is_file(): return False  # Older one-off clips still belong to the console.
            status, body, headers = audio_response(clip.read_bytes(), handler.headers.get('Range', ''))
            self.io._bytes(handler, status, body, 'audio/wav', headers)
            return True
        if not match or handler.command != 'POST': return False
        consumed = False
        try:
            origin = handler.server.auth.origin_for(handler.headers.get('Host', ''))
            if handler.headers.get('Origin', '').rstrip('/').casefold() != origin.casefold():
                raise PermissionError('A speech request must come from this workspace.')
            boot = self.read(handler, '', '/api/bootstrap')
            wanted = boot.get('csrf')
            if not isinstance(wanted, str) or not wanted or not secrets.compare_digest(wanted, handler.headers.get('X-Atlas-CSRF', '')):
                raise PermissionError('Refresh this page to reconnect.')
            length = int(handler.headers.get('Content-Length') or 0)
            if not 0 < length <= 4096: raise ValueError('Invalid speech request size')
            raw = handler.rfile.read(length); consumed = True
            if len(raw) != length: raise ValueError('Incomplete speech request')
            args = json.loads(raw)
            if not isinstance(args, dict) or set(args) - {'item_id', 'voice'}:
                raise ValueError('Supply a native message ID and optional voice only')
            item_id = args.get('item_id')
            if not isinstance(item_id, str) or not 0 < len(item_id) <= 256:
                raise ValueError('Name the reply to read')
            agent_prefix, room = match[1] or '', match[2]
            def page(cursor):
                query = {'limit': 200, 'direction': 'desc'}
                if cursor: query['cursor'] = cursor
                return self.read(handler, agent_prefix, '/api/room/' + room + '/history?' + urlencode(query))
            item = find_message(page, item_id)
            text = _item_text(item)
            speech = self.voice.speak(text, str(args.get('voice') or DEFAULT_VOICE)).as_json()
            speech.update(room=room, item_id=item_id, type=item.get('type'))
            self.io._reply(handler, 200, speech)
        except PermissionError as exc:
            if not consumed: handler._drain()
            self.io._reply(handler, 403, {'error':'speech_forbidden', 'message':str(exc)})
        except (ValueError, TypeError) as exc:
            handler.close_connection = True
            self.io._reply(handler, 400, {'error':'bad_speech', 'message':str(exc)})
        except SpeechLookupError as exc:
            handler.close_connection = True
            self.io._reply(handler, 404 if exc.code == 'item_unknown' else 503, {'error':exc.code, 'message':str(exc)})
        except (OSError, RemoteError, VoiceError):
            handler.close_connection = True
            self.io._reply(handler, 503, {'error':'speech_unavailable', 'message':'Voice is unavailable. Try again.'})
        return True
