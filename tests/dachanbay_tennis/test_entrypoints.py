import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from projects.dachanbay_tennis.__main__ import main


class EntryPoints(unittest.TestCase):
    def test_gui_modes_open_without_starting_booking(self):
        module = ModuleType('projects.dachanbay_tennis.assistant')
        module.main = Mock()
        with patch.dict(sys.modules, {module.__name__: module}), patch('common.desktop.require_tk'):
            for args in ([], ['watch'], ['capture']):
                main(args)
            self.assertEqual(module.main.call_count, 3)

    def test_query_dispatches_only_to_read_only_probe(self):
        with patch('projects.dachanbay_tennis.query.probe') as probe, \
                patch('common.desktop.require_tk', side_effect=AssertionError('query needs no GUI')):
            main(['query'])
            probe.assert_called_once_with()

    def test_payment_capture_dispatches_to_separate_window(self):
        module = ModuleType('projects.dachanbay_tennis.payment_capture')
        module.main = Mock()
        with patch.dict(sys.modules, {module.__name__: module}), patch('common.desktop.require_tk'):
            main(['capture-payment'])
            module.main.assert_called_once_with()
