"""Read-only local visualization fragments, isolated from workspace credentials."""
import html
import os
from pathlib import Path
import stat
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 1_000_000
CDNS = 'https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://esm.sh https://fonts.bunny.net https://fonts.googleapis.com https://fonts.gstatic.com https://unpkg.com'
CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' " + CDNS +
       "; style-src 'unsafe-inline' " + CDNS + "; img-src data: blob: " + CDNS +
       "; font-src data: " + CDNS + "; connect-src 'none'; frame-src 'none'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


class Visualizations:
    def __init__(self, roots, kit):
        self.roots = tuple(Path(root).expanduser().resolve(strict=True) for root in roots)
        kit = Path(kit)
        self.css = (kit / 'visualize.css').read_text()
        self.template = (kit / 'visualize.html').read_text()
        if '<!--__INLINE_VISUALIZATION_FRAGMENT__-->' not in self.template:
            raise ValueError('Visualization kit has no fragment slot')

    def document(self, value):
        path = Path(value)
        parent = path.parent.resolve(strict=True)
        # Only direct children of explicit output directories; no generic file server.
        if not path.is_absolute() or parent not in self.roots or path.suffix.lower() != '.html':
            raise ValueError('Unavailable visualization')
        root_fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError('Unavailable visualization')
                data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError('Unavailable visualization')
        finally:
            os.close(root_fd)
        fragment = data.decode('utf-8')
        title = html.escape(path.stem.replace('-', ' '))
        content = self.template.replace('<!--__INLINE_VISUALIZATION_FRAGMENT__-->', fragment)
        return ('<!doctype html><html lang="en" data-visualize-standalone><head>'
                '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta name="referrer" content="no-referrer"><title>' + title + '</title>'
                '<style>' + self.css + '</style></head><body>' + content + '</body></html>').encode()

    def handle(self, handler):
        url = urlsplit(handler.path)
        if url.path != '/api/visualizations/render':
            return False
        status = 200
        try:
            if handler.command not in ('GET', 'HEAD'):
                raise ValueError('Read only')
            args = parse_qs(url.query)
            if set(args) != {'path'} or len(args['path']) != 1:
                raise ValueError('Invalid path')
            body = self.document(args['path'][0])
        except (ValueError, OSError, UnicodeError):
            status = 404
            body = b'<!doctype html><meta charset="utf-8"><p>This visualization is not available on this host. Its source may belong to another agent.</p>'
        handler.send_response(status)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Security-Policy', CSP)
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Referrer-Policy', 'no-referrer')
        handler.end_headers()
        if handler.command != 'HEAD':
            handler.wfile.write(body)
        handler.close_connection = True
        return True
