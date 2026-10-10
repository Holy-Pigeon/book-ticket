from tests.support import private_storage_test
import datetime as dt
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from projects.dachanbay_tennis import core
from projects.dachanbay_tennis.core import Api, BookingError, Journal, choices, duplicate, submit, target_dates, validate_slot
from projects.dachanbay_tennis.capture_proxy import Proxy
from common.storage import protect

TARGET = ('2026-10-13', '21:00', '22:00')
SLOT = dict(start_time='21:00:00', end_time='22:00:00', status=1, price=6000,
            consumption_rule=1, duration_fee_type=3)
GRID = dict(mode=1, pay_min_booking_count=1, pay_max_booking_count=2,
            items=[dict(ground_id='court', ground_name='tennis', booking_durations=[SLOT])])


class Fake:
    credentials = {}

    def __init__(self, journal, fail=False):
        self.journal = journal
        self.fail = fail
        self.calls = []

    def profile(self):
        return {'require_verification': 1}

    def orders(self):
        return []

    def grid(self, *args):
        return GRID

    def venue(self, name, body):
        self.calls.append((name, body))
        if name == 'CheckIsViolation':
            return {'is_violation': False}
        assert self.journal.contains(TARGET), 'journal must precede POST'
        assert Journal(self.journal.path).contains(TARGET), 'journal must be durable'
        if self.fail:
            raise BookingError('ambiguous')
        return {'main_order_no': 'order123', 'need_payment': True}


class Tests(unittest.TestCase):
    def test_dates_and_target_matching(self):
        now = dt.datetime(2026,10,10,12,tzinfo=core.TZ)
        dates = [{'date': x} for x in ['2026-10-10','2026-10-13','2026-10-14','2026-10-15']]
        self.assertEqual(target_dates(dates, now), [TARGET, ('2026-10-14','19:00','20:00')])
        self.assertEqual(len(choices(GRID, TARGET)), 1)
        self.assertEqual(choices(GRID, ('2026-10-13','20:00','21:00')), [])

    def test_price_captcha_warning_and_limits(self):
        slot = choices(GRID, TARGET)[0]
        validate_slot(GRID, slot, 6000)
        for change in ({'price':6001}, {'consumption_rule':2}, {'status':5}, {'duration_fee_type':99}):
            with self.assertRaises(BookingError):
                validate_slot(GRID, {**slot, **change}, 6000)
        with self.assertRaises(BookingError):
            validate_slot({**GRID,'pay_min_booking_count':2}, slot, 6000)

    def test_order_duplicate_and_unknown_shape(self):
        self.assertTrue(duplicate([dict(date=TARGET[0],start_time='21:00',end_time='22:00',status=9)], TARGET))
        self.assertFalse(duplicate([dict(date=TARGET[0],start_time='19:00',end_time='20:00')], TARGET))
        with self.assertRaises(BookingError):
            duplicate([dict(date=TARGET[0])], TARGET)

    @private_storage_test
    def test_write_ahead_exact_body_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp)/'attempts.json')
            api = Fake(journal)
            submit(api, journal, 'tennis', TARGET, choices(GRID,TARGET)[0], 6000, threading.Event())
            self.assertEqual(api.calls[-1], ('PayBooking', {'scene':2,'mp_openid':'',
                'slots':[{'ground_id':'court','start_time':'2026-10-13 21:00:00',
                          'end_time':'2026-10-13 22:00:00'}],'confirm_booking_agreement':2}))
            self.assertTrue(Journal(journal.path).contains(TARGET))
            self.assertNotIn(b'order123', journal.path.read_bytes())
            with self.assertRaises(BookingError):
                submit(api, Journal(journal.path), 'tennis', TARGET, choices(GRID,TARGET)[0], 6000, threading.Event())
            self.assertEqual(sum(x[0]=='PayBooking' for x in api.calls),1)

    @private_storage_test
    def test_ambiguous_result_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp)/'attempts.json')
            api = Fake(journal,True)
            for _ in range(2):
                with self.assertRaises(BookingError):
                    submit(api,journal,'tennis',TARGET,choices(GRID,TARGET)[0],6000,threading.Event())
            self.assertEqual(sum(x[0]=='PayBooking' for x in api.calls),1)
            self.assertEqual(Journal(journal.path).records[Journal.key(TARGET)]['state'],'unknown')

    def test_identity_stop_and_violation_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp)/'attempts.json')
            for method, value in [('profile',{'require_verification':0}),('venue',{}),('venue',{'is_violation':True})]:
                api = Fake(journal)
                with patch.object(api,method,return_value=value):
                    with self.assertRaises(BookingError):
                        submit(api,journal,'tennis',TARGET,choices(GRID,TARGET)[0],6000,threading.Event())
                self.assertFalse(journal.contains(TARGET))
            stop = threading.Event(); stop.set()
            with self.assertRaises(BookingError):
                submit(Fake(journal),journal,'tennis',TARGET,choices(GRID,TARGET)[0],6000,stop)

    def test_corrupt_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'attempts.json'; path.write_text('bad')
            with self.assertRaises(BookingError): Journal(path)

    @private_storage_test
    def test_dpapi_roundtrip_no_plaintext(self):
        raw = b'LOCAL-FAKE-TOKEN-ONLY-FOR-TEST'
        encrypted = protect(raw)
        self.assertNotIn(raw, encrypted)
        self.assertEqual(protect(encrypted,True), raw)
        with tempfile.TemporaryDirectory() as tmp, patch.object(core,'DATA',Path(tmp)):
            core.save_credentials({'token':raw.decode(),'user_agent':'fake'})
            self.assertEqual(core.load_credentials()['token'],raw.decode())
            self.assertNotIn(raw,(Path(tmp)/'credentials.dpapi').read_bytes())


class MockResponse:
    status = 200
    def __init__(self, status=200, headers=None, data=None):
        self.status = status; self.headers = headers or {}; self.data = data or b'{}'
    def getheader(self, name): return self.headers.get(name)
    def read(self, size): return self.data


class ApiErrors(unittest.TestCase):
    def test_401_trpc_redirect_and_invalid_json(self):
        api = Api({'token':'fake-test-token'})
        for response in [MockResponse(401),MockResponse(302),
                         MockResponse(headers={'trpc-func-ret':'2'}),
                         MockResponse(headers={'trpc-error-msg':'secret must not be echoed'}),
                         MockResponse(data=b'invalid')]:
            with patch('projects.dachanbay_tennis.core.http.client.HTTPConnection') as factory:
                factory.return_value.getresponse.return_value = response
                with self.assertRaises(BookingError) as e: api.venue('GetGroundTypeList')
                self.assertNotIn('secret',str(e.exception))
                self.assertEqual(factory.return_value.request.call_count,1)


class Upstream(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length','0')))
        result=json.dumps({'path':self.path,'body':body.decode()}).encode()
        self.send_response(200); self.send_header('Content-Length',str(len(result))); self.end_headers()
        self.wfile.write(result)
    do_GET = do_POST


class ProxyTests(unittest.TestCase):
    def test_forward_target_capture_only_and_no_log(self):
        upstream = ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        threading.Thread(target=upstream.serve_forever,daemon=True).start()
        captured=[]; proxy=Proxy(captured.append,port=0); proxy.start()
        original=http.client.HTTPConnection
        try:
            def mapped(host,port,**kw): return original('127.0.0.1',upstream.server_port,**kw)
            with patch('projects.dachanbay_tennis.capture_proxy.http.client.HTTPConnection',side_effect=mapped):
                conn=original('127.0.0.1',proxy.server_port,timeout=5)
                conn.request('POST',f'http://{core.HOST}/api/h5-venue-api/GetGroundTypeList',b'{}',
                    {'spc-jwt-token':'FAKE-ONLY','Referer':f'http://{core.HOST}/index?mp_openid=fake-id',
                     'User-Agent':'fake-agent'})
                response=conn.getresponse(); self.assertEqual(response.status,200)
                self.assertEqual(json.loads(response.read())['body'],'{}'); conn.close()
                self.assertEqual(captured[0]['token'],'FAKE-ONLY')
                self.assertEqual(captured[0]['mp_openid'],'fake-id')
                for url in ('http://example.invalid/api/test',f'http://{core.HOST}/index'):
                    conn=original('127.0.0.1',proxy.server_port,timeout=5)
                    conn.request('GET',url,headers={'spc-jwt-token':'DO-NOT-CAPTURE'})
                    self.assertEqual(conn.getresponse().read()[:1],b'{'); conn.close()
                self.assertEqual(len(captured),1)
        finally:
            proxy.stop(); upstream.shutdown(); upstream.server_close()

    def test_connect_tunnel_without_decryption(self):
        server=socket.socket(); server.bind(('127.0.0.1',0)); server.listen()
        def echo():
            client,_=server.accept()
            with client: client.sendall(client.recv(100))
        threading.Thread(target=echo,daemon=True).start()
        captured=[]; proxy=Proxy(captured.append,port=0); proxy.start()
        try:
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=5) as sock:
                sock.sendall(f'CONNECT 127.0.0.1:{server.getsockname()[1]} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
                response=b''
                while b'\r\n\r\n' not in response: response += sock.recv(1)
                self.assertIn(b'200',response)
                sock.sendall(b'opaque TLS bytes'); self.assertEqual(sock.recv(100),b'opaque TLS bytes')
            self.assertEqual(captured,[])
        finally:
            proxy.stop(); server.close()


if __name__=='__main__': unittest.main(verbosity=2)
