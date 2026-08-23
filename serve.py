#!/usr/bin/env python3
"""Serve an exported Freeform board directory over HTTP.

Static files only: no listings beyond the board index, no writes, and nothing
outside the standard library — the same rule the exporter follows.

It exists because the viewer needs three things a naive file server does not
give it. Byte ranges, or video will not seek and Safari will not play it at
all. Conditional requests, or a 35 GB board re-downloads itself on every
visit instead of costing a handful of 304s. And a path resolver that refuses
to answer for anything outside the export directory.

    python3 serve.py ~/Desktop/boards
    python3 serve.py --host 0.0.0.0 --port 8080 /srv/boards
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import gzip
import hmac
import html
import io
import mimetypes
import os
import posixpath
import re
import signal
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0"

# Content types are declared rather than looked up, so a board serves the same
# way on Alpine as it does on macOS. Freeform's exports lean on a handful of
# formats the platform maps inconsistently or not at all — .heic above all.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    # board.md is meant to be read in the browser, not downloaded.
    ".md": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".flac": "audio/flac",
    ".caf": "audio/x-caf",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
}

# Worth compressing: text that is large enough to matter and small enough to
# hold in memory. Media is already compressed and is streamed untouched.
GZIP_TYPES = {
    "text/html", "text/plain", "text/css", "text/javascript",
    "application/json", "image/svg+xml",
}
GZIP_MIN = 1024
GZIP_MAX = 8 * 1024 * 1024

STREAM_CHUNK = 256 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

# The viewer is one self-contained page: its own inline script and style, its
# own images and video, and no network calls. Everything else is denied.
# 'unsafe-inline' is unavoidable — the generated HTML inlines both, and there
# is no request-time rewriting here to hand it a nonce.
CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


class Config:
    root = "/srv/boards"
    asset_max_age = 300
    auth = None          # b"user:pass", or None
    gzip = True
    log = True


CFG = Config()


def env_str(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in CONTENT_TYPES:
        return CONTENT_TYPES[ext]
    guess = mimetypes.guess_type(path)[0]
    # Freeform writes some previews with no extension at all; they are binary
    # plists, and octet-stream is the honest answer for them.
    return guess or "application/octet-stream"


def resolve(root, url_path):
    """Map a URL path to a real file under `root`, or return None.

    Containment is checked after resolving symlinks, so a link inside the
    export that points anywhere outside it is refused rather than followed.
    """
    path = urllib.parse.unquote(url_path, encoding="utf-8",
                                errors="surrogateescape")
    path = posixpath.normpath(path)
    parts = [p for p in path.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return None

    candidates = [parts]
    # A board exported on macOS can carry decomposed filenames ("Stu?ndenglass"),
    # and a copy of the export may arrive normalised the other way. Trying both
    # forms turns a silent 404 on one board into a hit.
    for form in ("NFC", "NFD"):
        alt = [unicodedata.normalize(form, p) for p in parts]
        if alt != parts:
            candidates.append(alt)

    for cand in candidates:
        full = os.path.join(root, *cand) if cand else root
        if not os.path.exists(full):
            continue
        real = os.path.realpath(full)
        if real != root and not real.startswith(root + os.sep):
            return None
        return real
    return None


def parse_range(header, size):
    """Return (start, end) inclusive for a single range, or None.

    Multi-range requests fall back to the whole file, which the spec allows
    and which no browser this serves ever asks for.
    """
    match = RANGE_RE.match(header.strip())
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if first == "":
        if last == "":
            return None
        length = min(int(last), size)
        if length <= 0:
            return None
        return size - length, size - 1
    start = int(first)
    if start >= size:
        return "unsatisfiable"
    end = int(last) if last else size - 1
    return start, min(end, size - 1)


def etag_matches(header, etag):
    if not header:
        return False
    if header.strip() == "*":
        return True
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def board_index(root):
    """A minimal listing of exported boards, for when there is no index.html.

    `export --board X` writes no top-level page, and a bare 404 at / is a
    confusing way to learn that.
    """
    rows = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not os.path.isfile(os.path.join(entry.path, "index.html")):
            continue
        href = urllib.parse.quote(entry.name) + "/"
        rows.append(f'<li><a href="{html.escape(href)}">'
                    f'{html.escape(entry.name)}</a></li>')
    body = "\n".join(rows) or "<li>No boards found in this directory.</li>"
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"color-scheme\" content=\"light dark\">"
        "<title>Freeform Boards</title><style>"
        "body{font:16px/1.6 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;"
        "max-width:44rem;margin:3rem auto;padding:0 1.25rem}"
        "h1{font-size:1.4rem;letter-spacing:-.02em}"
        "ul{list-style:none;padding:0}li{padding:.35rem 0}"
        "a{color:inherit}</style></head><body><h1>Boards</h1><ul>\n"
        f"{body}\n</ul></body></html>\n"
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = f"freeform/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ---- plumbing -------------------------------------------------------

    def version_string(self):
        # The base class appends sys_version and leaves a trailing space.
        return self.server_version

    def log_message(self, fmt, *args):  # noqa: A003 - base class name
        pass

    def log_error(self, fmt, *args):
        pass

    def access(self, status, size, started):
        if not CFG.log:
            return
        ms = (time.monotonic() - started) * 1000
        sys.stdout.write(
            f'{time.strftime("%Y-%m-%dT%H:%M:%S")} {self.address_string()} '
            f'"{self.command} {self.path}" {status} {size} {ms:.0f}ms\n'
        )

    def head(self, name, value):
        self.send_header(name, value)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)

    def fail(self, status, message, started, extra=None):
        body = f"{status.value} {message}\n".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.access(status.value, len(body), started)

    def authorised(self):
        if CFG.auth is None:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:].strip(), validate=True)
        except Exception:
            return False
        return hmac.compare_digest(raw, CFG.auth)

    # ---- entry points ---------------------------------------------------

    def do_GET(self):
        self.handle_request(body=True)

    def do_HEAD(self):
        self.handle_request(body=False)

    def do_POST(self):
        self.reject_method()

    def do_PUT(self):
        self.reject_method()

    def do_DELETE(self):
        self.reject_method()

    def do_OPTIONS(self):
        self.reject_method()

    def reject_method(self):
        started = time.monotonic()
        self.fail(HTTPStatus.METHOD_NOT_ALLOWED, "Method Not Allowed", started,
                  {"Allow": "GET, HEAD"})

    # ---- the request ----------------------------------------------------

    def handle_request(self, body):
        started = time.monotonic()
        try:
            url = urllib.parse.urlsplit(self.path)
        except ValueError:
            return self.fail(HTTPStatus.BAD_REQUEST, "Bad Request", started)

        # Unauthenticated on purpose: it reveals nothing and the container
        # healthcheck has no credentials to offer.
        if url.path == "/healthz":
            payload = b"ok\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(payload)
            return self.access(200, len(payload), started)

        if not self.authorised():
            return self.fail(
                HTTPStatus.UNAUTHORIZED, "Unauthorized", started,
                {"WWW-Authenticate": 'Basic realm="Freeform boards", charset="UTF-8"'})

        target = resolve(CFG.root, url.path)
        if target is None:
            return self.fail(HTTPStatus.NOT_FOUND, "Not Found", started)

        if os.path.isdir(target):
            if not url.path.endswith("/"):
                # url.path is still in its encoded form, straight off the
                # request line. Quoting it again turns %20 into %2520.
                location = url.path + "/"
                if url.query:
                    location += "?" + url.query
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.security_headers()
                self.end_headers()
                return self.access(301, 0, started)
            index = os.path.join(target, "index.html")
            at_root = target == CFG.root
            if os.path.isfile(index):
                # at_root lets this fall through instead of failing: a refresh
                # replaces the top index, and a shared mount can go on
                # claiming the old one exists for a moment after it is gone.
                # Answering with the generated listing beats a 404.
                if self.send_file(index, body, started, tolerate_missing=at_root):
                    return None
            if at_root:
                return self.send_bytes(board_index(CFG.root),
                                       "text/html; charset=utf-8", body,
                                       started, cache="no-cache")
            return self.fail(HTTPStatus.NOT_FOUND, "Not Found", started)

        if not os.path.isfile(target):
            return self.fail(HTTPStatus.NOT_FOUND, "Not Found", started)

        return self.send_file(target, body, started)

    def send_bytes(self, payload, ctype, body, started, cache):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.security_headers()
        self.end_headers()
        if body:
            self.wfile.write(payload)
        self.access(200, len(payload), started)

    def send_file(self, path, body, started, tolerate_missing=False):
        """Send one file. Returns False only when it could not be opened and
        the caller said it would handle that itself; nothing is sent then."""
        try:
            st = os.stat(path)
            handle = open(path, "rb")
        except OSError:
            if tolerate_missing:
                return False
            self.fail(HTTPStatus.NOT_FOUND, "Not Found", started)
            return True

        with handle:
            size = st.st_size
            ctype = content_type(path)
            base_type = ctype.split(";")[0].strip()
            is_html = base_type == "text/html"

            accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
            compress = (CFG.gzip and accepts_gzip and base_type in GZIP_TYPES
                        and GZIP_MIN <= size <= GZIP_MAX)

            # mtime, size and inode together. The inode matters on a real
            # filesystem, where replacing a file always yields a new one and
            # so a new ETag even if the clock is coarse. It buys nothing over
            # a macOS-to-Linux sshfs mount, which both rounds mtime to the
            # second and synthesises inode numbers per path — there, two
            # same-size versions written inside one second are
            # indistinguishable. A refresh writes them seconds apart at the
            # very least, so that stays theoretical.
            etag = (f'"{st.st_mtime_ns:x}-{size:x}-{st.st_ino:x}'
                    f'{"-gz" if compress else ""}"')
            last_modified = email.utils.formatdate(st.st_mtime, usegmt=True)
            # HTML and layout data must never be stale after a refresh; the
            # media they point at is worth holding on to between pans.
            cache = ("no-cache" if is_html or base_type == "application/json"
                     else f"public, max-age={CFG.asset_max_age}, must-revalidate")

            if self.not_modified(etag, st.st_mtime):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache)
                self.send_header("Vary", "Accept-Encoding")
                self.end_headers()
                self.access(304, 0, started)
                return True

            if compress:
                buffer = io.BytesIO()
                with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
                    gz.write(handle.read())
                payload = buffer.getvalue()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self.send_header("Cache-Control", cache)
                self.send_header("Vary", "Accept-Encoding")
                self.send_header("Accept-Ranges", "none")
                self.security_headers()
                self.end_headers()
                if body:
                    self.wfile.write(payload)
                self.access(200, len(payload), started)
                return True

            start, end = 0, size - 1
            status = HTTPStatus.OK
            range_header = self.headers.get("Range")
            if_range = self.headers.get("If-Range")
            # A stale If-Range means the file changed under a paused video;
            # answering with the whole new file is the correct repair.
            if range_header and (not if_range or etag_matches(if_range, etag)):
                parsed = parse_range(range_header, size)
                if parsed == "unsatisfiable":
                    self.fail(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                              "Range Not Satisfiable", started,
                              {"Content-Range": f"bytes */{size}"})
                    return True
                if parsed:
                    start, end = parsed
                    status = HTTPStatus.PARTIAL_CONTENT

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", cache)
            self.send_header("Vary", "Accept-Encoding")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.security_headers()
            self.end_headers()

            if not body:
                self.access(status.value, 0, started)
                return True

            sent = self.stream(handle, start, length)
            self.access(status.value, sent, started)
            return True

    def not_modified(self, etag, mtime):
        if etag_matches(self.headers.get("If-None-Match"), etag):
            return True
        if self.headers.get("If-None-Match"):
            return False
        since = self.headers.get("If-Modified-Since")
        if not since:
            return False
        try:
            when = email.utils.parsedate_to_datetime(since)
        except (TypeError, ValueError):
            return False
        if when is None:
            return False
        return int(mtime) <= int(when.timestamp())

    def stream(self, handle, start, length):
        """Copy `length` bytes to the socket, tolerating an aborted transfer.

        Seeking in a video abandons connections constantly; that is normal
        traffic here, not an error worth a traceback.
        """
        handle.seek(start)
        sent = 0
        try:
            while sent < length:
                chunk = handle.read(min(STREAM_CHUNK, length - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        return sent

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def parse_auth(value):
    if not value:
        return None
    if ":" not in value:
        sys.exit("--auth expects user:password")
    return value.encode("utf-8")


def main():
    parser = argparse.ArgumentParser(
        prog="serve.py",
        description="Serve an exported Freeform board directory over HTTP.")
    parser.add_argument("root", nargs="?",
                        default=env_str("FREEFORM_ROOT", "/srv/boards"),
                        help="directory holding the export (default: %(default)s)")
    parser.add_argument("--host", default=env_str("FREEFORM_HOST", "127.0.0.1"),
                        help="address to bind (default: %(default)s)")
    parser.add_argument("--port", type=int, default=env_int("FREEFORM_PORT", 8080),
                        help="port to bind (default: %(default)s)")
    parser.add_argument("--auth", default=env_str("FREEFORM_AUTH"),
                        metavar="USER:PASS",
                        help="require HTTP basic auth (only meaningful behind TLS)")
    parser.add_argument("--asset-max-age", type=int,
                        default=env_int("FREEFORM_ASSET_MAX_AGE", 300),
                        metavar="SECONDS",
                        help="how long browsers may reuse media without asking "
                             "(default: %(default)s)")
    parser.add_argument("--no-gzip", action="store_true",
                        help="never compress text responses")
    parser.add_argument("--quiet", action="store_true",
                        help="do not log requests")
    args = parser.parse_args()

    root = os.path.realpath(os.path.abspath(args.root))
    if not os.path.isdir(root):
        sys.exit(f"Not a directory: {root}\n"
                 f"Export one first:  python3 freeform.py export {args.root}")

    CFG.root = root
    CFG.asset_max_age = max(0, args.asset_max_age)
    CFG.auth = parse_auth(args.auth)
    CFG.gzip = not args.no_gzip
    CFG.log = not args.quiet

    family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    Server.address_family = family
    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as exc:
        sys.exit(f"Cannot bind {args.host}:{args.port} — {exc}")

    boards = sum(1 for e in os.scandir(root)
                 if e.is_dir() and not e.name.startswith("."))
    shown = args.host if args.host not in ("0.0.0.0", "::") else "localhost"
    print(f"freeform {VERSION} serving {root}", flush=True)
    print(f"  {boards} board folder(s)"
          f"{'  ·  basic auth on' if CFG.auth else ''}", flush=True)
    print(f"  http://{shown}:{args.port}/", flush=True)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        print("shutting down", flush=True)
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
