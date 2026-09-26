#!/usr/bin/env python3
"""Serve the site locally the way GitHub Pages serves it (manual testing and the quality gate).

    make serve                              # http://127.0.0.1:8000/  (make serve PORT=8001)
    python3 tests/pages_server.py [PORT]    # same; PORT 0 picks a free port

Unlike `python3 -m http.server`, every missing path, however deep and whatever its query string,
gets the branded 404.html as the body with HTTP status 404 (never a 200, never a redirect), and a
directory without index.html is a 404 instead of a file listing. Like GitHub Pages, a directory
requested without its trailing slash gets a 301 to the slash with the query string intact
(/place?id=x -> /place/?id=x). Responses are marked no-cache so edits show on reload.
Standard library only; binds to 127.0.0.1.
"""
import signal
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PagesHandler(SimpleHTTPRequestHandler):
    # Python 3.9's mimetypes lacks a few types; Chrome sniffs images but keep it explicit.
    extensions_map = dict(SimpleHTTPRequestHandler.extensions_map, **{
        ".webp": "image/webp", ".svg": "image/svg+xml", ".js": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".xml": "application/xml", ".txt": "text/plain"})

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)   # GitHub Pages never lists a directory
        return None

    def send_error(self, code, message=None, explain=None):
        # Like GitHub Pages: every missing path, however deep, gets 404.html with status 404
        page = ROOT / "404.html"
        if code != 404 or not page.is_file():
            return super().send_error(code, message, explain)
        self.log_error("code %d, message %s", code, message or "Not Found")
        body = page.read_bytes()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class PagesServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):   # browsers drop connections mid-response
            super().handle_error(request, client_address)


def main(argv):
    port = int(argv[0]) if argv else 8000
    try:
        server = PagesServer(("127.0.0.1", port), PagesHandler)
    except OSError as e:
        print("cannot listen on 127.0.0.1:%d (%s); try: make serve PORT=%d" % (port, e.strerror, port + 1), file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("Serving %s at http://127.0.0.1:%d/ (GitHub-Pages-like 404s; Ctrl-C to stop)"
          % (ROOT, server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
