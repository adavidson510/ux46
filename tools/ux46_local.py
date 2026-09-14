"""Standalone UX46: fresh local state, no installation network calls."""
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
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    defaults = {
        'registry.json': {'schema_version': 1, 'node_id': 'local', 'projects': []},
        'agents.json': {'schema_version': 1, 'agents': []},
        'config.json': {'schema_version': 1, 'agent_label': 'Codex', 'port': 8877,
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


def run(root, config, port=None):
    os.environ['ATLAS_REGISTRY'] = str(root/'registry.json')
    import atlas_console as console
    from ux46_workspace_api import WorkspaceAPI
    from constellation_store import Conflict, Unavailable
    modules = config.get('modules', {})
    args = console.build_parser().parse_args([
        '--port', str(port or config.get('port', 8877)),
        '--state-dir', str(root/'state'), '--registry', str(root/'registry.json'),
        '--agents-config', str(root/'agents.json'),
        '--local-agent-label', config.get('agent_label', 'Codex')])
    service = console.ConsoleService(args)
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
            if not path.startswith(('/api/constellation/', '/api/email/', '/api/schedule/',
                                    '/api/usage-report/', '/api/desktop-devices/')):
                return super()._api(method, path, query, decision)
            reads = {'catalog', 'review', 'lookup', 'get', 'health', 'changes', 'view', 'status'}
            if method not in ('GET', 'POST') or (method == 'GET' and path.rsplit('/', 1)[-1] not in reads):
                raise console.ApiError(HTTPStatus.METHOD_NOT_ALLOWED, 'bad_method', 'Use POST for changes')
            # Host, loopback, origin and CSRF were checked by ConsoleHandler.
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
    print(f'UX46: http://127.0.0.1:{server.server_port}/', flush=True)
    print(f'Private data: {root}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.workers.shutdown()
        service.agents.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Your agents. Your work. Your space.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create private configuration; never overwrite it')
    launch = commands.add_parser('run', help='Run the local browser workspace')
    launch.add_argument('--port', type=int)
    commands.add_parser('doctor', help='Check prerequisites without starting an agent')
    add = commands.add_parser('project-add', help='Explicitly register a project directory')
    add.add_argument('path', type=Path)
    add.add_argument('--name')
    args = parser.parse_args(argv)
    root = home()
    if args.command == 'doctor':
        print(json.dumps({'python': sys.version.split()[0], 'codex_on_path': bool(shutil.which('codex')),
                          'claude_on_path': bool(shutil.which('claude')),
                          'config_exists': (root/'config.json').exists(), 'platform': sys.platform}, indent=2))
        return 0
    config = initialize(root)
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
    return run(root, config, args.port)
