from tests.support import private_storage_test
import math
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
from common.errors import LocalError
from common.polling import backoff
from common.storage import project_data_dir, read_json, write_json, write_plain_json
from common.concurrency import SingleInstance


class CommonTests(unittest.TestCase):
    def test_sanitized_json_failed_replace_preserves_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sanitized.json'
            write_plain_json(path, {'status': 1})
            before = path.read_bytes()
            self.assertEqual(json.loads(before), {'status': 1})
            with patch('common.storage.os.replace', side_effect=OSError('TEST')):
                with self.assertRaises(OSError): write_plain_json(path, {'status': 2})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_backoff_stays_bounded_for_long_outages(self):
        self.assertEqual([backoff(i) for i in range(1, 8)], [5, 10, 20, 40, 60, 60, 60])
        self.assertEqual(backoff(100000), 60)
        for bad in [0, -1]:
            with self.assertRaises(ValueError): backoff(bad)
        with self.assertRaises(ValueError): backoff(1, base=math.nan)

    def test_project_data_is_separate_and_names_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'BOOK_TICKET_DATA_DIR': tmp}):
            self.assertEqual(project_data_dir('pkuszh'), Path(tmp).resolve() / 'pkuszh')
            self.assertNotEqual(project_data_dir('pkuszh'), project_data_dir('tennis'))
            for name in ['..', '../outside', 'a\\b', '']:
                with self.assertRaises(ValueError): project_data_dir(name)

    @private_storage_test
    def test_private_storage_and_failed_replace_preserve_previous_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nested' / 'record.dpapi'
            write_json(path, {'value': 'TEST_ONLY_SECRET'})
            before = path.read_bytes()
            self.assertNotIn(b'TEST_ONLY_SECRET', before)
            self.assertEqual(read_json(path), {'value': 'TEST_ONLY_SECRET'})
            with patch('common.storage.os.replace', side_effect=OSError('TEST')):
                with self.assertRaises(OSError): write_json(path, {'value': 'NEW'})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_instance_lock_blocks_duplicates_and_releases(self):
        name = 'test-' + uuid.uuid4().hex
        with SingleInstance(name):
            with self.assertRaises(LocalError):
                with SingleInstance(name): pass
            with SingleInstance(name + '-other'): pass
        with SingleInstance(name): pass
