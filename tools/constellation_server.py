#!/usr/bin/env python3
"""Constellation service for scoped agent clients. Exposes no email operations.

Bind to loopback behind an HTTPS private-network proxy, or use the owner-only
Unix socket. Agent bearer hashes/project grants are loaded from an explicit
operator-owned config on EVERY request so revocation needs no restart.
"""
import argparse
import hashlib
import hmac
import json
import os
import socketserver
import signal
import socket
import stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from constellation_store import Store, Principal, Conflict, Unavailable, encoded
from ux46_workspace_api import constellation_call


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def do_POST(self):
        self.close_connection=True
        try:
            if self.path!='/v1/call' or self.headers.get('Origin'):
                raise PermissionError('Agent endpoint does not accept browser requests')
            if self.server.local_principal:
                principal=self.server.local_principal
            else:
                config=json.loads(self.server.grants.read_text())
                token=self.headers.get('Authorization','')
                if not token.startswith('Bearer '):raise PermissionError('Authentication required')
                token_hash=hashlib.sha256(token[7:].encode()).hexdigest()
                found=next((p for p in config['principals'] if p.get('enabled',True)
                            and hmac.compare_digest(p['token_sha256'],token_hash)),None)
                if not found:raise PermissionError('Authentication refused')
                principal=Principal(found['name'],tuple(found['projects']),found.get('write',False))
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16000:raise ValueError('Request must be under 16 KB')
            payload=json.loads(self.rfile.read(length))
            result=constellation_call(self.server.store,principal,payload['operation'],payload.get('args',{}))
            self.reply(200,{'result':result,'principal':principal.name,'cache_ttl_seconds':300})
        except PermissionError:self.reply(403,{'error':'authentication_or_scope_refused'})
        except Conflict as exc:self.reply(409,{'error':'conflict','message':str(exc)})
        except Unavailable:self.reply(404,{'error':'unavailable_in_scope'})
        except (ValueError,KeyError,TypeError):self.reply(400,{'error':'invalid_request'})
        except Exception:self.reply(503,{'error':'service_unavailable'})

    def reply(self,status,payload):
        raw=encoded(payload)
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('Connection','close')
        self.end_headers();self.wfile.write(raw)


class UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True


def prepare_unix_path(path):
    """Hold one local listener lease; remove only an unreachable owner socket."""
    import fcntl
    fd=os.open(str(path)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    lease=os.fdopen(fd,'a+')
    try:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:info=path.lstat()
        except FileNotFoundError:return lease
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise RuntimeError('Existing path is not an owner-only service socket')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:probe.connect(str(path))
            except ConnectionRefusedError:pass
            else:raise RuntimeError('An existing listener still owns this socket')
        current=path.lstat()
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):
            raise RuntimeError('Socket changed during recovery')
        path.unlink()
        return lease
    except BaseException:
        lease.close();raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--socket');group.add_argument('--port',type=int)
    p.add_argument('--grants');p.add_argument('--local-principal',default='local-workspace')
    args=p.parse_args();os.umask(0o077)
    lease=None
    if args.socket:
        path=Path(args.socket).expanduser();path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        lease=prepare_unix_path(path)
        try:server=UnixServer(str(path),Handler)
        except BaseException:lease.close();raise
        socket_identity=path.lstat()
        server.local_principal=Principal(args.local_principal,('*',),True)
    else:
        if not args.grants:p.error('--grants is required for HTTP')
        server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
        server.local_principal=None;server.grants=Path(args.grants).expanduser()
    server.store=Store(args.db)
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print('Constellation ready; no model scheduler',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.server_close()
        if args.socket:
            try:
                current=path.lstat()
                if (current.st_dev,current.st_ino)==(socket_identity.st_dev,socket_identity.st_ino):path.unlink()
            except FileNotFoundError:pass
            finally:lease.close()


if __name__=='__main__':main()
