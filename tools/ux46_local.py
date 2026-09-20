"""Assemble a local workspace from its configuration and native agent adapter.

The installer supplies source; this module owns init/setup/run command routing.
Private state belongs under UX46_HOME, separate from the source you can share.
For a guided route through these pieces, start with docs/code-tour.md.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from http import HTTPStatus


def home():
    return Path(os.environ.get('UX46_HOME', '~/.ux46')).expanduser().resolve()


def initialize(root):
    # Defaults are for missing files only. open('x') below means "create, but
    # refuse to replace": running setup again must preserve the owner's settings.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    defaults = {
        'registry.json': {'schema_version': 1, 'node_id': 'local', 'projects': []},
        'agents.json': {'schema_version': 1, 'agents': []},
        'config.json': {'schema_version': 1, 'agent_label': 'Codex', 'agent': 'codex', 'port': 8877,
                       'execution_policy': 'workspace-write',
                       'modules': {'constellation': True, 'email': False, 'tell': False}},
    }
    for name, value in defaults.items():
        try:
            with (root/name).open('x', encoding='utf-8') as out:
                os.chmod(root/name, 0o600)
                json.dump(value, out, indent=2)
                out.write('\n')
        except FileExistsError:
            pass
    return json.loads((root/'config.json').read_text())


def run(root, config, port=None, open_browser=False):
    os.environ['ATLAS_REGISTRY'] = str(root/'registry.json')
    import atlas_console as console
    from ux46_workspace_api import WorkspaceAPI
    from constellation_store import Conflict, Unavailable
    modules = config.get('modules', {})
    selected = config.get('agent', 'codex')
    args = console.build_parser().parse_args([
        '--port', str(port or config.get('port', 8877)),
        '--state-dir', str(root/'state'), '--registry', str(root/'registry.json'),
        '--agents-config', str(root/'agents.json'),
        '--local-agent-label', config.get('agent_label', 'Codex'),
        '--execution-policy', config.get('execution_policy', 'preserve')])
    if selected == 'codex':
        args.codex_command = [config.get('cli') or 'codex', 'app-server']
    args.recovery_root = str(root)
    service = console.ConsoleService(args)
    claude_server = None
    if selected == 'claude':
        # Claude speaks a different session protocol. Give it an internal
        # loopback adapter so the browser can keep using one UX46 origin.
        # Port 0 asks the OS for a free port; it is not a public listener.
        import atlas_claude as claude
        import atlas_remote as remote
        import threading
        c = claude.build_parser().parse_args(['--agent-id','local','--agent-name',config.get('agent_label','Claude'),
            '--node','local','--registry',str(root/'registry.json'),'--state-dir',str(root/'claude'),
            '--cli',config.get('cli') or 'claude','--permission-mode','default','--quiet','--fresh-only'])
        c.recovery_root = str(root)
        service_claude=claude.ClaudeService(c)
        claude_server=claude.ClaudeServer(('127.0.0.1',0),service_claude)
        c.port=claude_server.server_port
        threading.Thread(target=claude_server.serve_forever,daemon=True).start()
        cp=claude_server.server_port
        service.agents.agents['local']=remote.Agent(agent_id='local',label=config.get('agent_label','Claude'),
            kind='remote',runtime='claude-code',node='local',console=remote.RemoteConsole(remote.DirectPort(cp),cp),
            capabilities=remote.DEFAULT_CAPABILITIES,transport_kind='in-process')
    elif selected == 'none':
        # Skipping provider setup still gives someone a workspace to explore.
        # Do not quietly substitute an available CLI for their explicit choice.
        service.agents.agents.pop('local',None)

    workspace = WorkspaceAPI(root/'workspace', modules=modules)
    import mimetypes
    for asset in (console.APP_DIR/'brand').iterdir():
        if asset.suffix in {'.png', '.svg', '.webmanifest'}:
            console.STATIC_FILES['/brand/'+asset.name] = ('brand/'+asset.name,
                mimetypes.guess_type(asset.name)[0] or 'application/octet-stream')

    class LocalHandler(console.ConsoleHandler):
        def _api(self, method, path, query, decision):
            if path == '/api/modules' and method == 'GET':
                return self._json(HTTPStatus.OK, modules)
            if path.startswith('/api/tell') and not modules.get('tell'):
                raise console.ApiError(HTTPStatus.NOT_FOUND, 'module_disabled', 'Tell is not configured')
            if selected == 'claude' and path != '/api/bootstrap':
                import atlas_remote as remote
                from urllib.parse import urlsplit
                if any(method in methods and pattern.fullmatch(path) for methods,pattern in remote.PROXY_ALLOWLIST):
                    return self._agent_api(method,'local',path,urlsplit(self.path).query,query,decision)
            if selected == 'none' and path == '/api/session-options':
                return self._json(HTTPStatus.OK, {'available':False,'blank':False,'projects':[],
                    'message':'Connect an agent with ux46 setup or ux46 connect.'})
            if selected == 'none' and method != 'GET' and path in ('/api/sessions','/api/connection/refresh'):
                raise console.ApiError(HTTPStatus.CONFLICT,'not_configured','Connect an agent first')
            if not path.startswith(('/api/constellation/', '/api/email/', '/api/schedule/',
                                    '/api/usage-report/', '/api/desktop-devices/')):
                return super()._api(method, path, query, decision)
            reads = {'catalog', 'review', 'lookup', 'get', 'health', 'changes', 'view', 'status'}
            if method not in ('GET', 'POST') or (method == 'GET' and path.rsplit('/', 1)[-1] not in reads):
                raise console.ApiError(HTTPStatus.METHOD_NOT_ALLOWED, 'bad_method', 'Use POST for changes')
            # ConsoleHandler checked host, loopback, origin and CSRF before
            # reaching here. CSRF ties a mutation to this UI, helping prevent
            # an unrelated web page from commanding the local app. New routes
            # must preserve that boundary, even if their buttons are hidden.
            values = {k: v[-1] for k, v in query.items()} if method == 'GET' else self._body()
            try:
                return self._json(HTTPStatus.OK, workspace.dispatch(path, values))
            except Conflict:
                raise console.ApiError(HTTPStatus.CONFLICT, 'conflict', 'Refresh before saving')
            except Unavailable:
                raise console.ApiError(HTTPStatus.NOT_FOUND, 'unavailable', 'Feature unavailable or disabled')
            except PermissionError:
                raise console.ApiError(HTTPStatus.FORBIDDEN, 'forbidden', 'Access refused')
            except (KeyError, TypeError, ValueError):
                raise console.ApiError(HTTPStatus.BAD_REQUEST, 'bad_request', 'Check the requested fields')
            except (OSError, sqlite3.Error):
                raise console.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, 'unavailable', 'Local store unavailable')

    server = console.ConsoleServer(('127.0.0.1', args.port), LocalHandler, service)
    from ux46_recovery import register_console
    register_console(root, server.server_port)
    print(f'UX46: http://127.0.0.1:{server.server_port}/', flush=True)
    if open_browser:
        import threading, webbrowser
        threading.Timer(.4,lambda:webbrowser.open(f'http://127.0.0.1:{server.server_port}/')).start()
    print(f'Private data: {root}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.workers.shutdown()
        service.agents.close()
        if claude_server:
            claude_server.shutdown();claude_server.server_close();service_claude.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Your agents. Your work. Your space.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create private configuration; never overwrite it')
    launch = commands.add_parser('run', help='Run the local browser workspace')
    launch.add_argument('--port', type=int)
    launch.add_argument('--open',action='store_true',help='Open the local workspace in your browser')
    doctor = commands.add_parser('doctor', help='Inspect this installation or explicitly recover its owned connections')
    doctor.add_argument('--check', action='store_true', help='Inspection only; never restart anything')
    doctor.add_argument('--json', action='store_true')
    doctor.add_argument('--agent'); doctor.add_argument('--room')
    operation = doctor.add_mutually_exclusive_group()
    operation.add_argument('--recover', action='store_true', help='Refresh the exact selected agent')
    operation.add_argument('--recover-all', action='store_true', help='Recover all configured UX46-owned services; may interrupt active owned work')
    doctor.add_argument('--request-id', help='Stable recovery operation ID; repeating it only reads its receipt')
    setup = commands.add_parser('setup', help='Configure this agent without importing history')
    setup.add_argument('--agent',choices=['auto','codex','claude','none'],default='auto')
    setup.add_argument('--name');setup.add_argument('--cli');setup.add_argument('--json',action='store_true')
    link=commands.add_parser('connect',help='Register a trusted local UX46-compatible adapter')
    link.add_argument('--id',required=True);link.add_argument('--name',required=True)
    link.add_argument('--runtime',required=True);link.add_argument('--port',required=True,type=int)
    add = commands.add_parser('project-add', help='Explicitly register a project directory')
    add.add_argument('path', type=Path)
    add.add_argument('--name')
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '--doctor': argv[0] = 'doctor'
    args = parser.parse_args(argv)
    root = home()
    if args.command == 'doctor':
        from ux46_doctor import inspect, render, successful
        from ux46_recovery import Coordinator
        if args.check and (args.recover or args.recover_all): parser.error('--check is inspection only')
        if args.recover and not args.agent: parser.error('--recover needs --agent ID')
        if args.recover_all and (args.agent or args.room): parser.error('--recover-all covers the installation; omit --agent and --room')
        if args.request_id and not (args.recover or args.recover_all): parser.error('--request-id needs an explicit recovery mode')
        try:
            if args.recover or args.recover_all:
                if args.recover_all and not args.json:
                    print('Recovering configured UX46-owned services. Active owned work may be interrupted; input will not be replayed.', flush=True)
                result = Coordinator(root).execute('all' if args.recover_all else 'agent', args.agent, args.request_id)
                if args.json: print(json.dumps(result, indent=2))
                else:
                    print(result['state'] + ': ' + result.get('message', ''))
                    for row in result.get('services', []) + result.get('connections', []):
                        print((row.get('id') or row.get('agent','')) + ': ' + row['state'])
                    print('Receipt: ' + result['id'])
                return 0 if result['state'] == 'complete' else 1
            result = inspect(root, args.agent, args.room)
        except (ValueError, OSError) as exc: parser.error(str(exc))
        if args.json: print(json.dumps(result, indent=2))
        else: render(result)
        return 0 if successful(result) else 1
    config = initialize(root)
    if args.command in ('setup','connect'):
        from ux46_setup import configure, connect
        try:
            result=(configure(root,config,agent=args.agent,name=args.name,cli=args.cli) if args.command=='setup'
                    else connect(root,identity=args.id,name=args.name,runtime=args.runtime,port=args.port))
        except ValueError as exc:parser.error(str(exc))
        print(json.dumps(result,indent=2))
        return 0
    if args.command == 'init':
        print(f'Ready: {root}. Start with: python3 tools/ux46 run')
        return 0
    if args.command == 'project-add':
        target = args.path.expanduser().resolve(strict=True)
        if not target.is_dir(): parser.error('Project must be a directory')
        identity = re.sub(r'[^a-z0-9._-]+', '-', target.name.lower()).strip('-')[:64]
        if not identity: parser.error('Choose a directory with a name')
        registry_path = root/'registry.json'
        registry = json.loads(registry_path.read_text())
        if any(p['id'] == identity or p['root'] == str(target) for p in registry['projects']):
            parser.error('Project already registered, or its name is already used')
        if (target/'sessions').exists() and any((target/'sessions').iterdir()):
            # Existing records may belong to a different registry or runtime.
            # Filing a project is not permission to import or take over its past.
            parser.error('This directory already has sessions. Initial registration does not import history.')
        manifest = target/'project.json'
        if not manifest.exists():
            with manifest.open('x') as out:
                json.dump({'schema_version': 1, 'id': identity, 'name': args.name or target.name,
                           'visibility': 'private', 'sources': []}, out, indent=2)
        registry['projects'].append({'id': identity, 'name': args.name or target.name, 'root': str(target)})
        temporary = registry_path.with_suffix('.pending')
        temporary.write_text(json.dumps(registry, indent=2)+'\n')
        temporary.chmod(0o600)
        temporary.replace(registry_path)
        print(f'Registered {identity}. New sessions will be saved in {target}/sessions.')
        return 0
    return run(root, config, args.port, args.open)
