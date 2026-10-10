import copy
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import os
from projects.pkuszh import core, capture


def fixture():
    url = 'https://' + core.HOST + core.SCHEDULE + '?' + urlencode({
        'deptId': 'test-only', 'deptName': '牙体牙髓科（补牙及根管治疗）',
        'startDate': '2026-10-11', 'endDate': '2026-10-16'})
    entry = {'request': {'method': 'GET', 'url': url, 'headers': [
        {'name': 'Cookie', 'value': 'session=TEST_ONLY'},
        {'name': 'User-Agent', 'value': 'test'},
        {'name': 'Other-Secret', 'value': 'DO_NOT_SAVE'}]},
        'response': {'content': {'text': 'DO_NOT_SAVE_MEDICAL_RESPONSE'}}}
    return {'log': {'entries': [entry]}}


class Tests(unittest.TestCase):
    def test_import_is_selective(self):
        result = core.select_request(fixture())
        self.assertEqual(set(result), {'url', 'headers'})
        self.assertEqual(set(result['headers']), {'cookie', 'user-agent'})
        self.assertNotIn('DO_NOT_SAVE', json.dumps(result))

    def test_post_not_selected(self):
        f = fixture()
        f['log']['entries'][0]['request']['method'] = 'POST'
        with self.assertRaises(core.LocalError):
            core.select_request(f)

    def test_other_domain_and_booking_path_rejected(self):
        url = fixture()['log']['entries'][0]['request']['url']
        for invalid in [url.replace(core.HOST, 'example.com'),
                        url.replace('scheduleNew/find', 'order/create'),
                        url.replace('https:', 'http:')]:
            with self.assertRaises(core.LocalError):
                core.validate_url(invalid)

    def test_sanitized_har_not_accepted_as_auth(self):
        f = fixture()
        f['log']['entries'][0]['request']['headers'] = []
        with self.assertRaises(core.LocalError):
            core.select_request(f)

    def test_other_specialty_rejected(self):
        url = 'https://' + core.HOST + core.SCHEDULE + '?' + urlencode({'deptId': 'a', 'deptName': '眼科'})
        with self.assertRaises(core.LocalError):
            core.validate_url(url)

    def test_header_injection_rejected(self):
        f = fixture()
        f['log']['entries'][0]['request']['headers'][0]['value'] += '\r\nTest: evil'
        with self.assertRaises(core.LocalError):
            core.select_request(f)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_roundtrip_and_no_plaintext_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)):
            value = core.select_request(fixture())
            core.save_session(value)
            self.assertEqual(core.load_session(), value)
            self.assertNotIn(b'TEST_ONLY', (Path(tmp) / 'session.dpapi').read_bytes())

    def test_wrong_hospital_stops_before_schedule(self):
        with patch.object(core, 'load_session', return_value=core.select_request(fixture())), \
             patch.object(core, 'get_json', return_value={'data': {'hospitalCode': 'OTHER'}}) as query:
            with self.assertRaises(core.LocalError):
                core.query_once()
            self.assertEqual(query.call_count, 1)

    def test_waitlist_and_nonopen_not_available(self):
        result = {'data': {'regInfos': [{'doctorName': '测试', 'scheduleInfos': [
            {'regLeaveCount': 1, 'regStatus': '1'},
            {'regLeaveCount': 0, 'regStatus': '1', 'waitingLock': '1'},
            {'regLeaveCount': 4, 'regStatus': '0'}]}]}}
        report = core.summarize(result)
        self.assertIn('其中 1 条可直接预约', report)
        self.assertEqual(report.count('当前不可直接预约'), 2)

    def test_network_read_allowlist(self):
        with patch.object(core.http.client, 'HTTPSConnection') as conn:
            with self.assertRaises(core.LocalError):
                core.get_json('/gateway/registration/appointment/order/create', {})
            conn.assert_not_called()

    @unittest.skipUnless(os.name == 'nt' and os.environ.get('BOOK_TICKET_LIVE_TESTS') == '1', 'Explicit opt-in live TLS check')
    def test_certificate_and_live_tls_forward_no_trust_install(self):
        # Public GET only. Root is trusted solely inside this test's SSLContext.
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)):
            certificate = capture.Certificate()
            self.assertFalse(certificate.installed)
            from cryptography import x509
            root = x509.load_der_x509_certificate(certificate.path.read_bytes())
            self.assertTrue(root.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            context = ssl.create_default_context()
            context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate.path.read_bytes()))
            saved, failures = [], []
            proxy = capture.Proxy(certificate, lambda: saved.append(True), lambda: failures.append(True), port=0)
            proxy.start()
            try:
                sock = socket.create_connection(proxy.server_address, timeout=20)
                sock.sendall(('CONNECT ' + core.HOST + ':443 HTTP/1.1\r\nHost: ' + core.HOST + ':443\r\n\r\n').encode())
                raw = b''
                while not raw.endswith(b'\r\n\r\n'):
                    raw += sock.recv(1)
                self.assertIn(b'200', raw)
                with context.wrap_socket(sock, server_hostname=core.HOST) as tls:
                    tls.sendall(('GET /patient/registration/ HTTP/1.1\r\nHost: ' + core.HOST + '\r\nConnection: close\r\n\r\n').encode())
                    received = b''
                    while True:
                        part = tls.recv(65536)
                        if not part:
                            break
                        received += part
                    self.assertTrue(received.startswith(b'HTTP/1.1 200'), received[:100])
                self.assertFalse(saved)
                self.assertFalse(failures)
                self.assertFalse((Path(tmp) / 'session.dpapi').exists())
            finally:
                proxy.stop()
                certificate.remove()
            self.assertFalse(certificate.path.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
