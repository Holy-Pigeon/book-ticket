import datetime as dt
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from common.polling import DeadlineStop
from projects.dachanbay_tennis import core, daily


class DailyTests(unittest.TestCase):
    def test_only_prepare_on_target_release_days_and_roll_past_window(self):
        now=dt.datetime(2026,10,12,10,tzinfo=core.TZ)
        self.assertEqual(daily.next_preparation(now),dt.datetime(2026,10,13,19,45,tzinfo=core.TZ))
        self.assertEqual(daily.next_preparation(now,8),now.replace(hour=19,minute=45))
        self.assertEqual(daily.next_preparation(now.replace(day=13,hour=20,minute=3)).day,14)
        self.assertEqual(daily.next_preparation(now.replace(day=14,hour=20,minute=3)).day,20)

    def test_fresh_browser_workflow_once_and_stop_without_next_day_retry(self):
        stop=threading.Event()
        events=[]
        def book(*args,**kwargs):
            events.append('book')
            self.assertIsNone(args[0])  # Login and cashier use the SAME page.
            self.assertEqual(args[1:3],('approved-synthetic-hash',None))
            self.assertTrue(kwargs['await_release'])
            stop.set()
        with patch.object(daily,'book_with_cashier',side_effect=book):
            daily.run_daily('approved-synthetic-hash',None,stop=stop,show=lambda s:None,
                            clock=lambda:dt.datetime(2026,10,13,19,45,tzinfo=core.TZ))
        self.assertEqual(events,['book'])

    def test_failed_workflow_stops_without_second_attempt(self):
        with patch.object(daily,'book_with_cashier',side_effect=core.BookingError('expired')) as book:
            with self.assertRaises(core.BookingError):
                daily.run_daily('approved-synthetic-hash',None,show=lambda s:None,stop=threading.Event(),
                                clock=lambda:dt.datetime(2026,10,13,19,45,tzinfo=core.TZ))
            book.assert_called_once()

    def test_file_stop_and_deadline_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'stop.request'
            stop=daily.FileStop(path)
            self.assertFalse(stop.is_set())
            path.write_text('stop')
            self.assertTrue(stop.is_set())
            self.assertTrue(stop.wait(30))
        event=threading.Event()
        bounded=DeadlineStop(event,10)
        with patch('common.polling.time.monotonic',return_value=11):
            self.assertTrue(bounded.is_set())
            self.assertTrue(bounded.wait(30))
        with patch('common.polling.time.monotonic',return_value=1):
            self.assertTrue(DeadlineStop(event,10,abort=lambda:True).is_set())
