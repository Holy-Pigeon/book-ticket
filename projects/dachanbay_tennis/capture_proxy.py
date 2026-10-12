"""Loopback-only forward proxy. HTTPS is tunneled without inspection."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import select
import socket
import threading
from urllib.parse import urlsplit, parse_qs
from .core import HOST

HOP = {"connection", "proxy-connection", "keep-alive", "proxy-authenticate",
       "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # URLs and request headers must never enter logs.

    def do_CONNECT(self):
        upstream = None
        sent, received = 0, 0
        host, port = "", 0
        try:
            host, port = self.path.rsplit(":", 1)
            port = int(port)
            upstream = socket.create_connection((host.strip("[]"), int(port)), timeout=15)
            if self.server.on_tunnel:
                try:
                    self.server.on_tunnel(host, port, "connected", 0, 0)
                except Exception:
                    pass
            self.send_response(200, "Connection established")
            self.end_headers()
            sockets = [self.connection, upstream]
            while not self.server.stopping.is_set():
                readable, _, _ = select.select(sockets, [], [], 1)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    if source is self.connection:
                        sent += len(data)
                    else:
                        received += len(data)
                    (upstream if source is self.connection else self.connection).sendall(data)
        except Exception:
            self.close_connection = True
        finally:
            if upstream:
                upstream.close()
                if self.server.on_tunnel:
                    try:
                        self.server.on_tunnel(host, port, "closed", sent, received)
                    except Exception:
                        pass
            self.close_connection = True

    def forward(self):
        conn = None
        try:
            url = urlsplit(self.path)
            if url.scheme != "http" or not url.hostname or url.username or url.password:
                self.send_error(400)
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_error(501, "Chunked request unsupported")
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 16 * 1024 * 1024:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else None
            if self.server.local_handler:
                local = self.server.local_handler(self.command, url, self.headers, body or b'')
                if local is not None:
                    status, mime, payload = local
                    self.send_response_only(status)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            value = None
            target_request = url.hostname.lower() == HOST and (url.port or 80) == 80 and url.path.startswith("/api/")
            if target_request:
                self.server.target_seen += 1
                token = self.headers.get("spc-jwt-token", "").strip()
                if token:
                    ref = urlsplit(self.headers.get("Referer", ""))
                    query = parse_qs(ref.query) if ref.hostname == HOST else {}
                    value = {"token": token, "user_agent": self.headers.get("User-Agent", ""),
                             "cookie": self.headers.get("Cookie", ""),
                             "mp_openid": query.get("mp_openid", [""])[0]}
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers["Connection"] = "close"
            conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=30)
            conn.request(self.command, url.path + ("?" + url.query if url.query else ""), body, headers)
            response = conn.getresponse()
            if value and response.status == 200 and not response.getheader("trpc-error-msg"):
                code = response.getheader("trpc-func-ret") or response.getheader("trpc-ret") or "0"
                if code == "0":
                    try:
                        self.server.on_credentials(value)
                    except Exception:
                        pass  # Credential storage must not break the original request.
            self.send_response_only(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP and key.lower() != "content-length":
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            captured_body = bytearray()
            response_size = 0
            while True:
                part = response.read(65536)
                if not part:
                    break
                response_size += len(part)
                if target_request and self.server.on_exchange and len(captured_body) < 262144:
                    captured_body.extend(part[:262144-len(captured_body)])
                self.wfile.write(part)
            if target_request and self.server.on_exchange:
                try:
                    self.server.on_exchange(self.command, url.path, body or b"", response.status,
                        dict(response.getheaders()), bytes(captured_body), response_size)
                except Exception:
                    pass  # Diagnostics must not affect the user's payment request.
        except Exception:
            # No exception text: network exceptions can embed URLs/credentials.
            try:
                self.send_error(502, "Forwarding failed")
            except Exception:
                pass
        finally:
            if conn:
                conn.close()
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = forward


class Proxy(ThreadingHTTPServer):
    daemon_threads = True
    # POSIX reuse permits immediate restart after TIME_WAIT without sharing an
    # active listener; Windows reuse can hijack a live listener, so use exclusive.
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, callback, port=8766, on_exchange=None, on_tunnel=None, local_handler=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.on_credentials = callback
        self.on_exchange = on_exchange
        self.on_tunnel = on_tunnel
        self.local_handler = local_handler
        self.stopping = threading.Event()
        self.target_seen = 0

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()
