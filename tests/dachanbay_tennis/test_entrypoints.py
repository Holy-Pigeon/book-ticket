import unittest
from unittest.mock import patch
from projects.dachanbay_tennis.__main__ import main


class EntryPoints(unittest.TestCase):
    def test_gui_modes_open_without_starting_booking(self):
        with patch('projects.dachanbay_tennis.assistant.main') as gui:
            for args in ([], ['watch'], ['capture']):
                main(args)
            self.assertEqual(gui.call_count, 3)

    def test_query_dispatches_only_to_read_only_probe(self):
        with patch('projects.dachanbay_tennis.assistant.probe') as probe:
            main(['query'])
            probe.assert_called_once_with()

    def test_payment_capture_dispatches_to_separate_window(self):
        with patch('projects.dachanbay_tennis.payment_capture.main') as capture:
            main(['capture-payment'])
            capture.assert_called_once_with()
