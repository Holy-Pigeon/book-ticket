import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
import contextlib
import io
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

    def test_booking_requires_explicit_agreement_and_budget_before_loading_credentials(self):
        with patch('projects.dachanbay_tennis.core.load_credentials',side_effect=AssertionError('no credentials should be read')):
            for args in (['book'],['book','--unlimited-price'],['book','--approve-notice','synthetic-hash']):
                with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                    main(args)

    def test_headless_booking_dispatch_preserves_unlimited_price_and_notice(self):
        from common.concurrency import SingleInstance
        with patch.object(SingleInstance,'__enter__'), patch.object(SingleInstance,'__exit__'), \
             patch('projects.dachanbay_tennis.core.load_credentials',return_value={'token':'fake-only'}), \
             patch('projects.dachanbay_tennis.workflow.book_with_cashier') as book, \
             patch('common.desktop.require_tk',side_effect=AssertionError('headless requires no GUI')):
            main(['book','--approve-notice','synthetic-hash','--unlimited-price','--manual-proxy'])
            self.assertEqual(book.call_args.args[1:],('synthetic-hash',None))
            self.assertTrue(book.call_args.kwargs['manual'])
            self.assertFalse(book.call_args.kwargs['await_release'])
