"""One-domain HTTPS inspection, opt-in temporary certificate, no trust changes on import."""
import datetime as dt
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import select
import socket
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from . import core

PORT = 8767
HOP = {'connection', 'proxy-connection', 'keep-alive', 'proxy-authenticate',
       'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'}


class Certificate:
    def __init__(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.expiry = now + dt.timedelta(hours=24)
        root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                'Local Hospital Capture - Temporary 24h')])
        root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
                .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(self.expiry)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                .sign(root_key, hashes.SHA256()))
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, core.HOST)]))
                .issuer_name(root_name).public_key(leaf_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(self.expiry)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(core.HOST)]), critical=False)
                .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(root_key, hashes.SHA256()))
        # Root signing key is never written to disk and not retained by this object.
        del root_key
        self.thumbprint = root.fingerprint(hashes.SHA1()).hex().upper()
        self.sha256 = root.fingerprint(hashes.SHA256()).hex().upper()
        core.DATA.mkdir(parents=True, exist_ok=True)
        self.path = core.DATA / ('capture-' + self.thumbprint + '.cer')
        self.path.write_bytes(root.public_bytes(serialization.Encoding.DER))
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.set_alpn_protocols(['http/1.1'])
        # Restrict the temporary key directory to this Windows user before writing a key.
        with tempfile.TemporaryDirectory(prefix='hospital-capture-') as directory:
            script = ('$p=$args[0];$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;'
                      '$acl=Get-Acl -LiteralPath $p;$acl.SetAccessRuleProtection($true,$false);'
                      '$rule=New-Object System.Security.AccessControl.FileSystemAccessRule('
                      '$sid,"FullControl","ContainerInherit,ObjectInherit","None","Allow");'
                      '$acl.AddAccessRule($rule);Set-Acl -LiteralPath $p -AclObject $acl')
            args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                    '& { param($target) ' + script.replace('$p=$args[0];', '$p=$target;') + ' }', directory]
            completed = subprocess.run(args, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if completed.returncode:
                raise core.LocalError('无法限制临时私钥目录的访问权限，未生成采集工具。')
            cert_path, key_path = Path(directory) / 'leaf.pem', Path(directory) / 'key.pem'
            cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM) + root.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            self.context.load_cert_chain(cert_path, key_path)
        del leaf_key
        self.installed = False

    def install(self):
        # Called only after an explicit user action and a trust-scope confirmation.
        r = subprocess.run(['certutil.exe', '-user', '-addstore', 'Root', str(self.path)],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode:
            raise core.LocalError('临时根证书未安装成功，采集未启动。')
        self.installed = True

    def remove(self):
        if self.installed:
            r = subprocess.run(['certutil.exe', '-user', '-delstore', 'Root', self.thumbprint],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode:
                raise core.LocalError('证书自动移除失败。请在 certmgr.msc 的受信任根证书中按窗口指纹移除此证书。')
            self.installed = False
        self.path.unlink(missing_ok=True)


def relay(handler, host, port):
    upstream = socket.create_connection((host, port), timeout=15)
    handler.server.track(upstream)
    try:
        handler.send_response(200, 'Connection established')
        handler.end_headers()
        handler.wfile.flush()
        while not handler.server.stopping.is_set():
            readable, _, _ = select.select([handler.connection, upstream], [], [], 1)
            for source in readable:
                part = source.recv(65536)
                if not part:
                    return
                (upstream if source is handler.connection else handler.connection).sendall(part)
    finally:
        handler.server.untrack(upstream)
        upstream.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    tls_target = False

    def setup(self):
        super().setup()
        self.connection.settimeout(40)
        self.server.track(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.untrack(self.connection)

    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        try:
            host, port = self.path.rsplit(':', 1)
            host, port = host.strip('[]').lower(), int(port)
            if port != 443:
                self.send_error(403, 'Only HTTPS tunnels are supported')
                return
            if host != core.HOST:
                relay(self, host, port)
                return
            self.send_response(200, 'Connection established')
            self.end_headers()
            self.wfile.flush()
            with self.server.certificate.context.wrap_socket(self.connection, server_side=True) as wrapped:
                TargetHandler(wrapped, self.client_address, self.server)
        except Exception:
            self.server.notice_failure()
        finally:
            self.close_connection = True

    def forward(self):
        conn = None
        response_started = False
        try:
            u = urlsplit(self.path)
            if self.tls_target:
                if u.scheme or u.netloc or not self.path.startswith('/') or self.path.startswith('//'):
                    self.send_error(400)
                    return
                if self.headers.get('Host', '').lower() not in {core.HOST, core.HOST + ':443'}:
                    self.send_error(421)
                    return
                host, port, target = core.HOST, 443, self.path
                conn = http.client.HTTPSConnection(host, port, timeout=25, context=ssl.create_default_context())
            else:
                if u.scheme != 'http' or not u.hostname or u.username or u.password:
                    self.send_error(400)
                    return
                host, port = u.hostname, u.port or 80
                target = u.path or '/'
                if u.query:
                    target += '?' + u.query
                conn = http.client.HTTPConnection(host, port, timeout=25)
            length = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Transfer-Encoding') or not 0 <= length <= 16 * 1024 * 1024:
                self.send_error(413, 'Unsupported request framing')
                return
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers['Connection'] = 'close'
            conn.request(self.command, target, body, headers)
            response = conn.getresponse()
            self.send_response_only(response.status, response.reason)
            for k, v in response.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header('Connection', 'close')
            self.end_headers()
            response_started = True
            if self.command != 'HEAD':
                while True:
                    part = response.read(65536)
                    if not part:
                        break
                    self.wfile.write(part)
            if self.tls_target and self.command == 'GET' and response.status == 200:
                try:
                    url = 'https://' + core.HOST + target
                    core.validate_url(url)
                    session_headers = {k.lower(): v for k, v in self.headers.items()
                                       if k.lower() in core.ALLOWED_HEADERS}
                    if session_headers.get('cookie') or session_headers.get('token'):
                        with self.server.save_lock:
                            core.save_session({'url': url, 'headers': session_headers})
                        self.server.on_saved()
                except core.LocalError:
                    pass
        except Exception:
            self.server.notice_failure()
            if not response_started:
                try:
                    self.send_error(502, 'Forwarding failed')
                except Exception:
                    pass
        finally:
            if conn:
                conn.close()
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = forward


class TargetHandler(Handler):
    tls_target = True

    def do_CONNECT(self):
        self.send_error(405)


class Proxy(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, certificate, on_saved, on_failure, port=PORT):
        self.certificate, self.on_saved, self.on_failure = certificate, on_saved, on_failure
        self.stopping = threading.Event()
        self.connections, self.connections_lock = set(), threading.Lock()
        self.save_lock = threading.Lock()
        self.failure_notified = False
        super().__init__(('127.0.0.1', port), Handler)

    def track(self, sock):
        with self.connections_lock:
            self.connections.add(sock)

    def untrack(self, sock):
        with self.connections_lock:
            self.connections.discard(sock)

    def notice_failure(self):
        if not self.failure_notified:
            self.failure_notified = True
            self.on_failure()

    def handle_error(self, request, client_address):
        self.notice_failure()  # Never print request details or exception strings.

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self):
        self.stopping.set()
        self.shutdown()
        with self.connections_lock:
            connections = list(self.connections)
        for sock in connections:
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError:
                pass
        self.server_close()
