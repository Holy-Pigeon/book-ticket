from tests.support import private_storage_test
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import hashlib
import unittest
from unittest.mock import patch, Mock

import os
from projects.pkuszh import core, booking, site_crypto, watch
from projects.pkuszh.api import BusinessError, RateLimitError


class BookingTests(unittest.TestCase):
    def test_crypto_matches_published_client_vector(self):
        vector = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/site_protocol.json').read_text(encoding='utf-8'))
        self.assertEqual(site_crypto.encrypt_payload(vector['payload']), vector['expected']['encrypted'])
        self.assertEqual(site_crypto.sign(vector['payload']), vector['expected']['sign'])

    def test_schedule_department_overrides_empty_doctor_group(self):
        b = booking.Booker.__new__(booking.Booker)
        b.api = type('Api', (), {'params': {'deptId': '270_03', 'deptName': '%E7%89%99%E4%BD%93%E7%89%99%E9%AB%93%E7%A7%91'}})()
        b.cap = booking.money('100')
        good = dict(deptId='1031', deptName='牙体牙髓科', regDate='2026-10-13', regLeaveCount='1', regStatus='1', regFee='0', treatFee='25')
        unrelated = dict(good, deptName='口腔颌面外科')
        doc = {'deptId': '', 'doctorId': 'TEST', 'scheduleInfos': [unrelated, good]}
        chosen, row = b.choose({'regInfos': [doc]})
        self.assertIs(row, good)
        self.assertEqual(chosen['deptId'], '1031')
        self.assertEqual(doc['deptId'], '')
        self.assertIsNone(b.choose({'regInfos': [dict(doc, scheduleInfos=[unrelated])]}))

    def test_fast_serial_cadence_and_bounded_read_backoff(self):
        self.assertEqual(watch.interval(None), 0.1)
        self.assertEqual(watch.retry_delay(BusinessError(core.SCHEDULE, '-1'), 2), 2)
        self.assertEqual(watch.retry_delay(RateLimitError(30), 1), 30)
        self.assertIsNone(watch.retry_delay(BusinessError('/gateway/registration/doctor/time/find', '-1'), 1))

    def test_worker_uses_response_then_point_one_wait_and_stop(self):
        events = []
        class Stop:
            done = False
            waits = 0
            def is_set(self): return self.done
            def set(self): self.done = True
            def wait(self, delay):
                events.append(('wait', delay))
                self.waits += 1
                if self.waits == 3: self.done = True
        class FakeBooker:
            def __init__(self): self.api = self
            def setup(self): pass
            def schedules(self):
                events.append(('response', None))
                return {'regInfos': []}
            def attempt(self, data): return None
            def choose(self, data): return None
        w = watch.Watch.__new__(watch.Watch)
        w.stop, w.report = Stop(), lambda msg: None
        with patch.object(watch, 'Booker', FakeBooker):
            w.worker()
        self.assertEqual(events, [('response', None), ('wait', 0.1)] * 3)

    def test_split_error_recovery_does_not_retry_consent_or_submission(self):
        path = '/gateway/registration/doctor/time/find'
        self.assertEqual(watch.preflight_retry_delay(BusinessError(path, '-1'), 1), 5)
        self.assertEqual(watch.preflight_retry_delay(BusinessError(path, '-1'), 3), 20)
        self.assertIsNone(watch.preflight_retry_delay(BusinessError(path, '-1'), 4))
        self.assertIsNone(watch.preflight_retry_delay(BusinessError('/gateway/registration/support/beforeRegister/window', '-1'), 1))
        b = booking.Booker.__new__(booking.Booker)
        b.api = type('Api', (), {'params': {'deptId': '270_03', 'deptName': '牙体牙髓科'}})()
        b.cap = booking.money('100')
        row = dict(deptId='1031', deptName='牙体牙髓科', regDate='2026-10-13', regLeaveCount='1', regStatus='1', regFee='0', treatFee='25', scheduleId='SCHEDULE')
        doc = {'doctorId': 'TEST', 'scheduleInfos': [row]}
        data = {'regInfos': [doc]}
        self.assertIsNotNone(b.choose(data))
        b.defer(doc, row, 5)
        self.assertIsNone(b.choose(data))
        self.assertNotIn('TEST SECRET', str(BusinessError(path, '-1', 'TEST SECRET')))

    def test_worker_recovers_split_read_failure_but_stops_if_journal_exists(self):
        for journal_exists in [False, True]:
            events = []
            class Stop:
                done = False
                def is_set(self): return self.done
                def set(self): self.done = True
                def wait(self, delay): events.append(('wait', delay))
            stop = Stop()
            class FakeBooker:
                attempts = 0
                def __init__(self): self.api = self
                def setup(self): pass
                def schedules(self):
                    events.append(('query', None))
                    return {'regInfos': []}
                def choose(self, data):
                    return ({'doctorName': 'TEST'}, {'regDate': '2026-10-13', 'shiftName': '上午'})
                def defer(self, doc, row, delay): events.append(('defer', delay))
                def attempt(self, data):
                    self.attempts += 1
                    if self.attempts == 1:
                        raise BusinessError('/gateway/registration/doctor/time/find', '-1')
                    stop.set()
                    return None
            w = watch.Watch.__new__(watch.Watch)
            w.stop, w.report = stop, lambda msg: None
            with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)), patch.object(watch, 'Booker', FakeBooker):
                if journal_exists: (Path(tmp) / 'booking-journal.dpapi').write_bytes(b'TEST')
                w.worker()
            self.assertEqual(sum(e[0] == 'query' for e in events), 1 if journal_exists else 2)
            self.assertEqual(('wait', 5) in events, not journal_exists)

    def basic(self):
        b = booking.Booker.__new__(booking.Booker)
        b.choose = lambda d: ({'doctorName': 'TEST'}, {'regDate': '2026-10-17', 'shiftName': '上午'})
        b.prepare = lambda doc, row: {'startTime': '09:00', 'endTime': '09:30'}
        return b

    @private_storage_test
    def test_ambiguous_submission_never_replayed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)), \
                patch.object(booking, 'Mutex', contextlib.nullcontext):
            b = self.basic()
            calls = []
            def submit(payload):
                calls.append(True)
                raise TimeoutError()
            b.submit = submit
            for _ in range(2):
                with self.assertRaises(core.LocalError):
                    b.attempt({})
            self.assertEqual(len(calls), 1)
            self.assertEqual(booking.read_private('booking-journal.dpapi')['state'], 'unknown')

    @private_storage_test
    def test_success_single_submission_and_encrypted_journal(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)), \
                patch.object(booking, 'Mutex', contextlib.nullcontext):
            b = self.basic()
            calls = []
            def submit(payload):
                calls.append(True)
                return {'resultCode': '0', 'data': {'registerMode': 1, 'orderId': 'TEST_ONLY_SECRET'}}
            b.submit = submit
            self.assertIn('锁号成功', b.attempt({}))
            with self.assertRaises(core.LocalError):
                b.attempt({})
            self.assertEqual(len(calls), 1)
            self.assertNotIn(b'TEST_ONLY_SECRET', (Path(tmp) / 'booking-journal.dpapi').read_bytes())

    def test_unavailable_slot_does_not_write_submission(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'DATA', Path(tmp)), \
                patch.object(booking, 'Mutex', contextlib.nullcontext):
            b = self.basic()
            b.prepare = lambda doc, row: None
            b.submit = lambda p: self.fail('must not submit')
            self.assertIsNone(b.attempt({}))
            self.assertFalse((Path(tmp) / 'booking-journal.dpapi').exists())

    def test_fees_reject_nonfinite_negative_or_unknown(self):
        for fee in ['NaN', 'Infinity', '-1', None, 'invalid']:
            with self.assertRaises(core.LocalError):
                booking.money(fee)

    def test_waitlist_not_normal_booking(self):
        self.assertFalse(booking.available({'regLeaveCount': '0', 'regStatus': '1', 'waitingLock': '1'}))
        self.assertFalse(booking.available({'regLeaveCount': '2', 'regStatus': '1', 'allowExtraAppoint': '1'}))
        self.assertTrue(booking.available({'regLeaveCount': '2', 'regStatus': '1'}))

    def test_normal_preparation_preserves_patient_consent_and_no_payment(self):
        flow = {'title': '剩余生物样本捐赠知情同意书', 'content': 'TEST CONSENT',
                'type': '2', 'needSignature': '', 'btnName2': '同意', 'btnValue2': '1', 'eventId': 'EVENT'}
        calls = []
        class FakeApi:
            params = {'deptId': '1032'}
            identity = {'tenantId': '01647'}
            session = {'headers': {'user-agent': 'MicroMessenger TEST'}}
            fail_cost = False
            def request(self, method, path, params=None, body=None):
                calls.append((method, path))
                if path.endswith('/register/limit/info'):
                    return []
                if path.endswith('/doctor/time/find'):
                    return [{'periodId': 'PERIOD', 'startTime': '09:00', 'endTime': '09:30', 'regLeaveCount': '1'}]
                if path.endswith('/beforeRegister/window'):
                    return flow
                if path.endswith('/svobject/find'):
                    return [{'svObjectId': '01', 'svObject': '医保', 'isInsuran': '1'}]
                if path.endswith('/cost/find'):
                    if self.fail_cost:
                        raise BusinessError(path, '-1')
                    return {'regFee': '0', 'treatFee': '33', 'yhFee': '0'}
                if path.endswith('/history/judge'):
                    return {'flag': '0'}
                if path.endswith('/current/find') or path.endswith('/unPaidOrder/judge'):
                    return []
                raise AssertionError('unexpected endpoint')
        b = booking.Booker.__new__(booking.Booker)
        b.api, b.cap = FakeApi(), booking.money('100')
        b.card = {'bindingId': 'TEST', 'patId': 'TEST', 'birthday': '2000-01-01'}
        b.patient = {'healthCardNo': 'TEST', 'patName': 'TEST', 'patIdno': 'TEST', 'phoneNo': 'TEST'}
        b.branch = {'branchCode': 'HZ010', 'branchName': '北京大学深圳医院'}
        b.settings = {'service': '医保', 'donation': 'agree',
                      'consent_hash': hashlib.sha256(flow['content'].encode()).hexdigest()}
        b.configs = [{'dataType': 'HOSPITAL_ORDER_CONFIG', 'mateName': 'registration.patient.flow.verify', 'value': '1'}]
        payload = b.prepare({'deptId': '1032', 'doctorId': 'TEST', 'doctorName': 'TEST'},
            {'deptName': '牙体牙髓科', 'regDate': '2026-10-17', 'scheduleId': 'SCHEDULE',
             'timeFlag': '1', 'shiftName': '上午', 'regFee': '0', 'treatFee': '33'})
        self.assertEqual(payload['svObjectId'], '01')
        self.assertEqual(json.loads(payload['beforeDiagnosisExtend'])['btnValue'], '1')
        self.assertEqual(payload['periodId'], 'PERIOD')
        self.assertFalse(any('/pay' in path or '/order/create' in path for _, path in calls))
        # Published selectServiceObj takes the national insurance branch before
        # getServiceObjectYhFee; it retains the selected service and schedule fees.
        b.online_insurance, b.insurance_city = True, '440302'
        b.configs += [{'dataType': t, 'mateName': n, 'value': v} for t, n, v in [
            ('PAYMENT_WEIXIN', 'online.medicare.pay', '1'),
            ('WEIXIN_ONLINE_MEDICARE_CONFIG', 'country.insurance.mode', '1'),
            ('HOSPITAL_ORDER_CONFIG', 'country.insurance.no.verify', '1')]]
        b.api.fail_cost = True
        calls.clear()
        doc = {'deptId': '1032', 'doctorId': 'TEST', 'doctorName': 'TEST'}
        row = {'deptName': '牙体牙髓科', 'regDate': '2026-10-17', 'scheduleId': 'SCHEDULE',
               'timeFlag': '1', 'shiftName': '上午', 'regFee': '0', 'treatFee': '33'}
        payload = b.prepare(doc, row)
        self.assertEqual(payload['svObjectId'], '01')
        self.assertEqual(payload['svObjectName'], '医保')
        self.assertEqual(payload['treatFee'], '33')
        self.assertEqual(b.order_path, booking.INSURANCE_ORDER_PATH)
        self.assertEqual(payload['medicareSettleLogId'], 'ElectronicMedicalInsurance')
        self.assertEqual(payload['insuranFee'], '0')
        self.assertEqual(payload['scheduleIdFromSchedule'], 'SCHEDULE')
        self.assertFalse(any(path.endswith('/cost/find') for _, path in calls))
        self.assertFalse(any('/pay' in path or '/order/create' in path for _, path in calls))
        b.online_insurance = False
        with self.assertRaises(BusinessError):
            b.prepare(doc, row)
        b.online_insurance = True
        b.cap = booking.money('25')
        with self.assertRaises(core.LocalError):
            b.prepare(doc, row)
        b.cap = booking.money('100')
        flow['content'] = 'CHANGED CONSENT'
        with self.assertRaises(core.LocalError):
            b.prepare({'deptId': '1032', 'doctorId': 'TEST', 'doctorName': 'TEST'},
                {'deptName': '牙体牙髓科', 'regDate': '2026-10-17', 'scheduleId': 'SCHEDULE',
                 'timeFlag': '1', 'shiftName': '上午', 'regFee': '0', 'treatFee': '33'})

    def test_create_protocol_matches_standard_and_insurance_frontend_branches(self):
        for path in [booking.ORDER_PATH, booking.INSURANCE_ORDER_PATH]:
            b = booking.Booker.__new__(booking.Booker)
            b.api = type('Api', (), {'session': {'headers': {}}})()
            b.order_path = path
            conn = Mock()
            response = conn.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"resultCode":"0","data":{"registerMode":1}}'
            payload = {'svObjectId': '01', 'svObjectName': '医保'}
            with patch.object(booking.http.client, 'HTTPSConnection', return_value=conn):
                b.submit(payload)
            args = conn.request.call_args.args
            self.assertEqual(args[:2], ('POST', path))
            body = json.loads(args[2])
            self.assertEqual(body, payload if path == booking.INSURANCE_ORDER_PATH else site_crypto.encrypt_payload(payload))
            self.assertEqual(conn.request.call_count, 1)




if __name__ == '__main__':
    unittest.main(verbosity=2)
