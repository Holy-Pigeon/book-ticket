import contextlib
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common.errors import LocalError
from common.system_proxy import TemporaryProxy, WindowsProxy, MacProxy, proxy_session, recover_proxy
from tests.support import private_storage_test


class Backend:
    def __init__(self):
        self.state = {'server': 'original', 'flags': 8}
        self.changes = []
    def snapshot(self): return deepcopy(self.state)
    def local(self, port): return {'server': 'loopback', 'flags': 3}
    def apply(self, value):
        self.changes.append(value)
        self.state = deepcopy(value)


class ProxyRecovery(unittest.TestCase):
    @private_storage_test
    def test_restore_on_success_error_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'recovery.dpapi'
            for fail in (False, True):
                backend = Backend()
                before = backend.snapshot()
                try:
                    with TemporaryProxy(path, backend=backend):
                        self.assertTrue(path.exists())
                        self.assertNotEqual(backend.snapshot(), before)
                        if fail: raise RuntimeError('test failure')
                except RuntimeError:
                    self.assertTrue(fail)
                self.assertEqual(backend.snapshot(), before)
                self.assertFalse(path.exists())
            guard = TemporaryProxy(path, backend=backend)
            guard.__enter__()  # Simulate a process that did not run __exit__.
            with patch('common.system_proxy.TemporaryProxy', return_value=guard):
                self.assertTrue(recover_proxy(path))
            self.assertEqual(backend.snapshot(), before)
            self.assertFalse(recover_proxy(path))

    @private_storage_test
    def test_do_not_overwrite_another_proxy_change_or_recovery_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'recovery.dpapi'
            backend = Backend()
            guard = TemporaryProxy(path, backend=backend)
            guard.__enter__()
            with self.assertRaises(LocalError):
                TemporaryProxy(path, backend=backend).__enter__()
            backend.state = {'server': 'someone-else', 'flags': 3}
            with self.assertRaises(LocalError): guard.restore()
            self.assertTrue(path.exists())
            self.assertEqual(backend.state['server'], 'someone-else')

    @private_storage_test
    def test_apply_failure_rolls_back_and_manual_restoration_precedes_listener_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'recovery.dpapi'
            backend = Backend()
            before = backend.snapshot()
            real_apply = backend.apply
            def apply(value):
                real_apply(value)
                if value['server'] == 'loopback': raise LocalError('synthetic failure')
            backend.apply = apply
            with self.assertRaises(LocalError):
                TemporaryProxy(path, backend=backend).__enter__()
            self.assertEqual(backend.snapshot(), before)
            self.assertFalse(path.exists())
            events = []
            with proxy_session(path, manual=True, show=lambda s: None,
                               restored=lambda: events.append('restore')):
                events.append('forward')
            events.append('stop')
            self.assertEqual(events, ['forward', 'restore', 'stop'])

    def test_windows_http_only_and_mac_explicit_service(self):
        self.assertEqual(WindowsProxy().local(8766)['server'], 'http=127.0.0.1:8766')
        for name in (None, '', '-Wi-Fi'):
            with self.assertRaises(LocalError): MacProxy(name)
        backend = MacProxy('Wi-Fi')
        before = {'http': {'Enabled':'No','Server':'old.proxy','Port':'8080'},
                  'https': {'Enabled':'Yes','Server':'company','Port':'8443'},
                  'pac': {'Enabled':'Yes','URL':'https://example.invalid/proxy.pac'},
                  'discovery': {'Auto Proxy Discovery':'On'}}
        with patch.object(backend, 'snapshot', return_value=deepcopy(before)):
            local = backend.local(8766)
        self.assertEqual(local['https'], before['https'])
        self.assertEqual(local['pac']['URL'], before['pac']['URL'])
        calls = []
        with patch.object(backend, '_run', side_effect=lambda *args: calls.append(args)):
            backend.apply(before)
        self.assertIn(('-setwebproxy', 'old.proxy', '8080'), calls)
        self.assertFalse(any('securewebproxy' in str(call) for call in calls))
        self.assertIn(('-setautoproxystate', 'on'), calls)
