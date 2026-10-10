import http.client
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from projects.pkuszh import api, core, watch


class NetworkTests(unittest.TestCase):
    def request(self, fault=None, status=200, raw=b'{"resultCode":0,"data":{}}'):
        client = api.Api.__new__(api.Api)
        client.session = {'headers': {}}
        conn = Mock()
        if fault:
            conn.request.side_effect = fault
        response = conn.getresponse.return_value
        response.status = status
        response.read.return_value = raw
        with patch.object(api.http.client, 'HTTPSConnection', return_value=conn):
            try:
                return client.request('GET', core.SCHEDULE)
            finally:
                self.assertEqual(conn.request.call_count, 1)
                conn.close.assert_called_once()

    def test_network_types_without_sensitive_exception_text(self):
        for fault, kind in [(TimeoutError('TEST_SECRET'), '网络超时'),
                            (http.client.RemoteDisconnected('TEST_SECRET'), '连接中断'),
                            (http.client.IncompleteRead(b'TEST_SECRET'), '连接中断'),
                            (OSError('TEST_SECRET'), '网络连接失败')]:
            with self.subTest(kind=kind), self.assertRaises(api.TransientRequestError) as result:
                self.request(fault=fault)
            self.assertEqual(result.exception.kind, kind)
            self.assertNotIn('TEST_SECRET', str(result.exception))

    def test_server_outage_is_transient_but_auth_tls_and_format_are_fatal(self):
        for status in [502, 503, 504]:
            with self.assertRaises(api.TransientRequestError): self.request(status=status)
        with self.assertRaises(core.LocalError) as auth: self.request(status=401)
        self.assertNotIsInstance(auth.exception, api.TransientRequestError)
        with self.assertRaises(core.LocalError) as tls: self.request(fault=ssl.SSLError('TEST_SECRET'))
        self.assertNotIsInstance(tls.exception, api.TransientRequestError)
        self.assertNotIn('TEST_SECRET', str(tls.exception))
        with self.assertRaises(api.ResponseFormatError): self.request(raw=b'<html>TEST_SECRET</html>')

    def test_outage_recovers_after_more_than_four_failures_and_remains_stoppable(self):
        for cancel_on_wait in [False, True]:
            events = []
            messages = []
            class Stop:
                done = False
                def is_set(self): return self.done
                def set(self): self.done = True
                def wait(self, delay):
                    events.append(delay)
                    if cancel_on_wait: self.done = True
            stop = Stop()
            class FakeBooker:
                queries = 0
                def __init__(self): self.api = self
                def setup(self): pass
                def schedules(self):
                    self.queries += 1
                    if self.queries <= 6:
                        raise api.TransientRequestError(core.SCHEDULE, '网络超时')
                    return {'regInfos': []}
                def choose(self, data): return None
                def attempt(self, data):
                    stop.set()
                    return None
            worker = watch.Watch.__new__(watch.Watch)
            worker.stop, worker.report = stop, messages.append
            with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)), patch.object(watch, 'Booker', FakeBooker):
                worker.worker()
                self.assertFalse((Path(tmp) / 'booking-journal.dpapi').exists())
            if cancel_on_wait:
                self.assertEqual(events, [5])
            else:
                self.assertEqual(events, [5, 10, 20, 40, 60, 60, 0.1])
                self.assertTrue(any('已恢复正常' in m for m in messages))
                self.assertFalse(any('守候已停止' in m for m in messages))

    def test_retries_are_scoped_to_availability_reads(self):
        error = api.TransientRequestError('/gateway/registration/support/beforeRegister/window', '网络超时')
        self.assertIsNone(watch.retry_delay(error, 1))
        self.assertIsNone(watch.preflight_retry_delay(error, 1))
        for count in [1, 5, 100000]:
            self.assertLessEqual(watch.retry_delay(api.TransientRequestError(core.SCHEDULE, '网络超时'), count), 60)


if __name__ == '__main__':
    unittest.main(verbosity=2)
