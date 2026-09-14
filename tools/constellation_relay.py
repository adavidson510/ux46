"""Owner-only local socket to a scoped HTTPS client; credential arrives on stdin."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import socketserver
import sys
from http.server import BaseHTTPRequestHandler
from constellation_server import prepare_unix_path

def client_types():
    path=Path(__file__).resolve().parents[1]/'skills/constellation/scripts/client.py'
    spec=importlib.util.spec_from_file_location('constellation_client',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.Client,module.Refused

Client,Refused=client_types()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        self.close_connection=True
        try:
            if self.path!='/v1/call' or self.headers.get('Origin'):raise Refused('Refused',403)
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16000:raise ValueError('Request size')
            payload=json.loads(self.rfile.read(length))
            result=self.server.client.request(payload['operation'],payload.get('args',{}))
            status=200
        except Refused as exc:
            status=exc.status if exc.status in (400,401,403,404,409) else 503
            result={'error':'upstream_refused' if status!=503 else 'upstream_unavailable'}
        except (ValueError,KeyError,TypeError):status=400;result={'error':'invalid_request'}
        except Exception:status=503;result={'error':'upstream_unavailable'}
        raw=json.dumps(result).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
        self.end_headers();self.wfile.write(raw)

class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket',required=True);parser.add_argument('--config',required=True)
    args=parser.parse_args();os.umask(0o077)
    credential=sys.stdin.buffer.read(4097).decode().strip()
    if not 32<=len(credential)<=4096:raise ValueError('Credential unavailable')
    os.environ.pop('OP_SERVICE_ACCOUNT_TOKEN',None)
    config=json.loads(Path(args.config).read_text())
    if config.get('socket') or not config.get('url'):raise ValueError('Relay requires HTTPS configuration')
    path=Path(args.socket).expanduser();path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    lease=prepare_unix_path(path)
    server=Server(str(path),Handler);identity=path.lstat()
    server.client=Client(config,credential=credential)
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.server_close()
        try:
            current=path.lstat()
            if (current.st_ino,current.st_dev)==(identity.st_ino,identity.st_dev):path.unlink()
        except FileNotFoundError:pass
        lease.close()

if __name__=='__main__':main()
