import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from common import paths, platforms, storage
from common.concurrency import SingleInstance
from common.errors import LocalError
from tests.support import MemoryKeychain


class PlatformTests(unittest.TestCase):
    def test_cli_help_and_missing_session_work_with_ascii_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, PYTHONIOENCODING='ascii', BOOK_TICKET_DATA_DIR=directory)
            for project in ['projects.pkuszh', 'projects.dachanbay_tennis']:
                with self.subTest(project=project):
                    result = subprocess.run([sys.executable, '-m', project, '--help'],
                                            capture_output=True, env=environment, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
                    self.assertIn('预约', result.stdout.decode('utf-8'))
                    result = subprocess.run([sys.executable, '-m', project, 'query'],
                                            capture_output=True, env=environment, timeout=10)
                    self.assertEqual(result.returncode, 1)
                    self.assertNotIn(b'Traceback', result.stderr)
                    self.assertIn('登录', result.stderr.decode('utf-8'))

    def test_native_data_roots(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': 'local-test'}), \
                patch('common.paths.system', return_value='windows'):
            self.assertEqual(paths.application_data_dir(), Path('local-test/book-ticket'))
        with patch('common.paths.system', return_value='macos'), patch('common.paths.Path.home', return_value=Path('home-test')):
            self.assertEqual(paths.application_data_dir(), Path('home-test/Library/Application Support/book-ticket'))

    def test_macos_settings_and_http_https_instructions(self):
        with patch('common.platforms.system', return_value='macos'), patch('common.platforms.subprocess.run') as run:
            platforms.open_proxy_settings()
            self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-b', 'com.apple.systempreferences'])
            self.assertNotIn('creationflags', run.call_args.kwargs)
            self.assertNotIn('(HTTPS)', platforms.proxy_instructions(8766))
            self.assertIn('(HTTPS)', platforms.proxy_instructions(8767, https=True))
            run.side_effect = OSError('TEST')
            with self.assertRaises(LocalError): platforms.open_proxy_settings()

    def test_mac_private_directory_permission_before_key_write(self):
        directory = Mock()
        with patch('common.platforms.system', return_value='macos'), patch('common.platforms.Path', return_value=directory):
            platforms.restrict_private_directory('temporary-owned-dir')
            directory.chmod.assert_called_once_with(0o700)

    def test_mac_certificate_user_scope_and_exact_cleanup_with_retry(self):
        trust = platforms.UserCertificateTrust('synthetic.cer', 'A' * 40, 'example.test')
        with patch('common.platforms.system', return_value='macos'), \
                patch('common.platforms.subprocess.run', return_value=Mock(returncode=0)) as run:
            trust.install()
            add = run.call_args.args[0]
            self.assertEqual(add[:7], ['/usr/bin/security', 'add-trusted-cert', '-r', 'trustRoot', '-p', 'ssl', '-s'])
            self.assertEqual(add[7], 'example.test')
            self.assertNotIn('-d', add)
            self.assertNotIn('creationflags', run.call_args.kwargs)
            run.return_value.returncode = 1
            with self.assertRaises(LocalError): trust.remove()
            self.assertTrue(trust.installed)
            run.return_value.returncode = 0
            trust.remove()
            self.assertEqual(run.call_args.args[0], ['/usr/bin/security', 'delete-certificate', '-t', '-Z', 'A' * 40, str(trust.keychain)])
            self.assertFalse(trust.installed)

    def test_failed_mac_trust_install_still_removes_added_certificate(self):
        trust = platforms.UserCertificateTrust('synthetic.cer', 'B' * 40, 'example.test')
        with patch('common.platforms.system', return_value='macos'), patch('common.platforms.subprocess.run') as run:
            run.side_effect = subprocess.CalledProcessError(1, 'test')
            with self.assertRaises(LocalError): trust.install()
            self.assertTrue(trust.cleanup_required)
            run.side_effect = None
            run.return_value.returncode = 0
            trust.remove()
            self.assertFalse(trust.cleanup_required)

    def test_windows_certificate_stays_in_current_user_store(self):
        trust = platforms.UserCertificateTrust('synthetic.cer', 'C' * 40, 'example.test')
        with patch('common.platforms.system', return_value='windows'), \
                patch('common.platforms.command_options', return_value={}), \
                patch('common.platforms.subprocess.run', return_value=Mock(returncode=0)) as run:
            trust.install()
            self.assertEqual(run.call_args.args[0][:4], ['certutil.exe', '-user', '-addstore', 'Root'])
            trust.remove()
            self.assertEqual(run.call_args.args[0], ['certutil.exe', '-user', '-delstore', 'Root', 'C' * 40])

    def test_process_lock_blocks_other_process_and_recovers_after_crash(self):
        name = 'test-process-' + uuid.uuid4().hex
        code = ('import sys; from common.concurrency import SingleInstance; '
                'lock=SingleInstance(sys.argv[1]); lock.__enter__(); '
                'print("ready",flush=True); sys.stdin.read()')
        process = subprocess.Popen([sys.executable, '-c', code, name], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with self.assertRaises(LocalError):
                with SingleInstance(name): pass
        finally:
            process.kill()
            process.communicate(timeout=10)
        with SingleInstance(name): pass


class MacStorageTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.keychain = MemoryKeychain()
        self.stack.enter_context(patch('common.storage.system', return_value='macos'))
        self.stack.enter_context(patch('common.storage._mac_keyring', return_value=self.keychain))

    def test_persistent_key_and_tamper_detection(self):
        encrypted = storage.protect(b'TEST-SECRET')
        self.assertTrue(encrypted.startswith(storage.MAC_PREFIX))
        self.assertNotIn(b'TEST-SECRET', encrypted)
        original_key = dict(self.keychain.items)
        self.assertEqual(storage.protect(encrypted, True), b'TEST-SECRET')
        storage.protect(b'SECOND-SECRET')
        self.assertEqual(self.keychain.items, original_key)
        with self.assertRaises(LocalError): storage.protect(encrypted[:-1] + b'!', True)

    def test_missing_key_on_read_never_creates_replacement(self):
        encrypted = storage.protect(b'TEST')
        self.keychain.items.clear()
        with self.assertRaises(LocalError): storage.protect(encrypted, True)
        self.assertEqual(self.keychain.items, {})

    def test_denied_keychain_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private.dpapi'
            storage.write_json(path, {'state': 'unknown'})
            before = path.read_bytes()
            with patch.object(self.keychain, 'get_password', side_effect=RuntimeError('PRIVATE TEST DETAIL')):
                with self.assertRaises(LocalError) as error: storage.write_json(path, {'state': 'new'})
            self.assertNotIn('PRIVATE TEST DETAIL', str(error.exception))
            self.assertEqual(path.read_bytes(), before)

    def test_windows_blob_rejected_without_creating_key(self):
        with self.assertRaises(LocalError): storage.protect(b'legacy-dpapi-test', True)
        self.assertEqual(self.keychain.items, {})

    @unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('BOOK_TICKET_KEYCHAIN_TESTS') == '1', 'Opt-in native Keychain check')
    def test_native_keychain_roundtrip(self):
        # Unique synthetic service; never accesses the real application master key.
        service = 'book-ticket-test-' + uuid.uuid4().hex
        self.stack.close()
        backend = storage._mac_keyring()
        with patch.object(storage, 'KEYCHAIN_SERVICE', service):
            try:
                encrypted = storage.protect(b'SYNTHETIC-KEYCHAIN-TEST')
                self.assertEqual(storage.protect(encrypted, True), b'SYNTHETIC-KEYCHAIN-TEST')
            finally:
                if backend.get_password(service, storage.KEYCHAIN_ACCOUNT):
                    backend.delete_password(service, storage.KEYCHAIN_ACCOUNT)
