from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
from common.errors import LocalError
from projects.pkuszh import core
from projects.pkuszh.configure import configure
from projects.pkuszh.booking import read_private
import os


class FakeApi:
    params = {'deptName': quote('牙体牙髓科')}
    def verify(self): pass
    def cards(self):
        return [{'available': True, 'bindingId': 'TEST', 'patId': 'TEST',
                 'healthCardNo': 'TEST', 'patName': 'TEST', 'patIdno': 'TEST', 'phoneNo': 'TEST'}]
    def schedules(self):
        return {'regInfos': [{'doctorId': 'TEST', 'scheduleInfos': [{'deptName': '牙体牙髓科',
                'deptId': '1031', 'regDate': '2026-10-13', 'shiftName': '上午'}]}]}
    def request(self, method, path, params=None, body=None):
        if path != '/gateway/registration/support/beforeRegister/window' or method != 'POST':
            raise AssertionError('Initialization must never submit an order')
        return {'title': '剩余生物样本捐赠知情同意书', 'content': '<p>TEST CURRENT NOTICE</p>',
                'type': '2', 'needSignature': '', 'btnName1': '不同意', 'btnName2': '同意'}


class ConfigureTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_fresh_local_configuration_preserves_explicit_refusal(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)), patch('projects.pkuszh.configure.Api', FakeApi):
            shown = []
            configure('医保', '100', ask=lambda prompt: '不同意', show=shown.append)
            prefs = read_private('preferences.dpapi')
            self.assertEqual(prefs['donation'], 'decline')
            self.assertEqual(prefs['service'], '医保')
            self.assertEqual(prefs['fee_cap'], '100')
            self.assertTrue(any('TEST CURRENT NOTICE' in line for line in shown))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['preferences.dpapi'])

    def test_missing_donation_choice_does_not_write_preferences(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)), patch('projects.pkuszh.configure.Api', FakeApi):
            with self.assertRaises(LocalError):
                configure('医保', '100', ask=lambda prompt: '', show=lambda line: None)
            self.assertEqual(list(Path(tmp).iterdir()), [])
