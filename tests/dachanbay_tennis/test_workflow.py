import contextlib
from copy import deepcopy
import datetime as dt
import http.client
import json
import re
import shutil
import subprocess
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from projects.dachanbay_tennis import core, workflow, cashier, release_watch
from projects.dachanbay_tennis.capture_proxy import Proxy
from tests.dachanbay_tennis.test_api import Fake, GRID, SLOT, TARGET
from tests.support import private_storage_test

NOW = dt.datetime(2026,10,12,19,59,45,tzinfo=core.TZ)
RELEASE_NOW = dt.datetime(2026,10,6,19,59,45,tzinfo=core.TZ)


class BookingApi(Fake):
    credentials = {'token':'SYNTHETIC-LOGIN-ONLY'}
    def tennis(self): return {'ground_type_id':'tennis'}
    def notices(self, type_id): return 'Synthetic current agreement'
    def dates(self, type_id): return [{'date': TARGET[0]}]


class WorkflowTests(unittest.TestCase):
    def test_prices_and_untrusted_numeric_inputs(self):
        self.assertEqual(workflow.fee_cap('60.25'), 6025)
        for amount in ('NaN','Infinity','0','-1','60.001',True):
            with self.assertRaises(core.BookingError): workflow.fee_cap(amount)
        for amount in (float('nan'),float('inf'),-1,1.5,True):
            grid = deepcopy(GRID)
            grid['items'][0]['booking_durations'][0]['price'] = amount
            with self.assertRaises(core.BookingError): core.choices(grid,TARGET)

    @private_storage_test
    def test_skip_invalid_court_and_over_budget_choose_valid_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = core.Journal(Path(tmp)/'journal.dpapi')
            api = BookingApi(journal)
            grid = deepcopy(GRID)
            grid['items'] = [dict(ground_id='captcha',booking_durations=[{**SLOT,'consumption_rule':2}]),
                             dict(ground_id='expensive',booking_durations=[{**SLOT,'price':9000}]),
                             dict(ground_id='ordinary',ground_name='court B',booking_durations=[SLOT])]
            with patch.object(api,'grid',return_value=grid):
                result = workflow.reserve_one(api,workflow.notice_digest(api.notices('tennis')),6000,journal,now=NOW)
            post = api.calls[-1]
            self.assertEqual(post[0],'PayBooking')
            self.assertEqual(post[1]['slots'][0]['ground_id'],'ordinary')
            self.assertEqual(result['ground_name'],'court B')
            self.assertEqual(result['price_cents'],6000)
            self.assertEqual(sum(x[0]=='PayBooking' for x in api.calls),1)
            self.assertIsNone(workflow.reserve_one(api,workflow.notice_digest(api.notices('tennis')),None,journal,now=NOW))

    @private_storage_test
    def test_changed_terms_or_fresh_price_prevent_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = core.Journal(Path(tmp)/'journal.dpapi')
            api = BookingApi(journal)
            approval = workflow.notice_digest(api.notices('tennis'))
            with patch.object(api,'notices',side_effect=['Synthetic current agreement','changed']):
                with self.assertRaises(core.BookingError): workflow.reserve_one(api,approval,6000,journal,now=NOW)
            fresh = deepcopy(GRID)
            fresh['items'][0]['booking_durations'][0]['price'] = 8000
            with patch.object(api,'grid',side_effect=[GRID,fresh]):
                with self.assertRaises(core.BookingError): workflow.reserve_one(api,approval,6000,journal,now=NOW)
            self.assertFalse(journal.records)
            self.assertEqual(api.calls,[])

    @private_storage_test
    def test_unlimited_budget_and_fresh_price_in_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = core.Journal(Path(tmp)/'journal.dpapi')
            api = BookingApi(journal)
            fresh = deepcopy(GRID)
            fresh['items'][0]['booking_durations'][0]['price'] = 8000
            with patch.object(api,'grid',side_effect=[GRID,fresh]):
                result = workflow.reserve_one(api,workflow.notice_digest(api.notices('tennis')),None,journal,now=NOW)
            self.assertEqual(result['price_cents'],8000)


class HandoffTests(unittest.TestCase):
    def test_origin_session_nonce_expiry_and_one_claim(self):
        handoff = cashier.Handoff(BookingApi.credentials)
        def request(action, token='SYNTHETIC-LOGIN-ONLY', origin=None):
            headers = {'spc-jwt-token':token, 'Origin':origin or 'http://'+core.HOST}
            return handoff.handle('POST',urlsplit(f'http://{core.HOST}/__book_ticket/{handoff.nonce}/{action}'),headers,b'')
        self.assertEqual(request('ready',token='other-account')[0],403)
        self.assertEqual(request('ready',origin='https://else.invalid')[0],403)
        self.assertFalse(handoff.ready.is_set())
        self.assertEqual(request('claim')[0],409)
        self.assertEqual(request('ready')[0],200)
        self.assertEqual(request('claim')[0],202)
        handoff.publish({'response':{'main_order_no':'synthetic-order','need_payment':True,'url_params':'signed-fake'}})
        html = handoff.handle('GET',urlsplit(handoff.url),{},b'')[2]
        self.assertNotIn(b'SYNTHETIC-LOGIN',html)
        self.assertNotIn(b'signed-fake',html)
        self.assertNotIn(b'CancelOrder',html)
        self.assertNotIn(b'SuccessPayOrderUpdate',html)
        self.assertEqual(request('claim')[0],200)
        self.assertIsNone(handoff.packet)
        self.assertEqual(request('claim')[0],409)
        self.assertEqual(request('launched')[0],200)
        self.assertTrue(handoff.launched.is_set())
        handoff.deadline = 0
        self.assertEqual(request('ready')[0],410)

    def test_proxy_serves_one_shot_page_without_upstream_request(self):
        handoff = cashier.Handoff(BookingApi.credentials)
        proxy = Proxy(lambda value: None, port=0,local_handler=handoff.handle)
        proxy.start()
        try:
            with self.assertRaises(OSError):
                Proxy(lambda value:None, port=proxy.server_port)
            conn = http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=5)
            conn.request('GET',handoff.url)
            response = conn.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(response.getheader('Cache-Control'),'no-store')
            self.assertIn(cashier.ASSET.encode(),response.read())
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=5)
            conn.request('POST',f'http://{core.HOST}/__book_ticket/{handoff.nonce}/ready',
                         headers={'Origin':'http://'+core.HOST,'spc-jwt-token':'SYNTHETIC-LOGIN-ONLY'})
            self.assertEqual(conn.getresponse().status,200)
            self.assertTrue(handoff.ready.is_set())
            conn.close()
        finally: proxy.stop()

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime not installed')
    def test_browser_script_calls_official_sdk_once_without_confirmation_or_cancel(self):
        script = re.search(r'<script type="module">(.*?)</script>',cashier.page('synthetic-nonce').decode(),re.S)[1]
        script = re.sub(r'import .*?;','const {cashier, profile, user} = globalThis.sdk;',script,count=1)
        harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
(async()=>{
for (const mode of ['paid','free','wrong-session','renewed-session']) {
  const requests=[], calls=[], redirects=[];
  let tick=0,reads=0;
  const paid=mode==='paid'||mode==='renewed-session';
  const response={main_order_no:'synthetic-order',need_payment:paid,url_params:'synthetic-signed-value'};
  const context={console,URLSearchParams,Date:class extends Date {static now(){return tick}},
    setTimeout(fn,delay){tick+=delay;fn()},document:{body:{append(){}},createElement(){return {remove(){}}}},
    window:{location:{assign(p){redirects.push(p)}}},
    fetch:async(url,options)=>{
      requests.push(url.split('/').pop());
      if (options.headers['spc-jwt-token']==='synthetic-expired') return {ok:false,status:403};
      assert.equal(options.headers['spc-jwt-token'],'synthetic-jwt');
      if (mode==='wrong-session') return {ok:false,status:403};
      return {ok:true,status:200,json:async()=>({response,mp_openid:''})};
    },
    sdk:{profile:()=>({getJwt:async(options)=>{assert.equal(options.autoRenew,false);reads++;return mode==='renewed-session'&&reads===1?'synthetic-expired':'synthetic-jwt'}}),
      user:()=>({setMpOpenid(value){assert.equal(value,'')}}),
      cashier:async(...args)=>{calls.push(args);assert.equal(args[0],response);assert.equal(args.length,4)}}};
  await new vm.Script('(async()=>{'+SOURCE+'})()').runInNewContext(context);
  assert.equal(calls.length,paid?1:0);
  assert.deepEqual(requests,mode==='wrong-session'?['ready']:mode==='renewed-session'?['ready','ready','claim','launched']:['ready','claim','launched']);
  assert.equal(redirects.length,0); // Only a REAL SDK success callback may redirect.
  if (paid) {
    calls[0][1].router.push({path:'/venue_success',query:{main_order_no:'synthetic-order'}});
    assert.equal(redirects[0],'/venue_success?main_order_no=synthetic-order');
  }
}
})().catch(error=>{console.error(error);process.exit(1)});
'''
        harness = harness.replace('SOURCE',json.dumps(script))
        result = subprocess.run([shutil.which('node'),'-e',harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    @private_storage_test
    def test_real_workflow_ready_before_post_then_single_delivery_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = core.Journal(Path(tmp)/'journal.dpapi')
            api = BookingApi(journal)
            events = []
            errors = []
            original = cashier.Handoff
            holder = {}
            def create_proxy(callback, **kwargs):
                value = Proxy(callback, port=0, **kwargs)
                holder['proxy'] = value
                return value
            def create(credentials,timeout,**kwargs):
                value = original(credentials,timeout,**kwargs)
                holder['handoff'] = value
                return value
            @contextlib.contextmanager
            def settings(*args):
                events.append('proxy-on')
                yield
                events.append('proxy-restored')
            def show(text):
                if text.startswith('http://'):
                    handoff = holder['handoff']
                    def browser():
                        port = holder['proxy'].server_port
                        conn = http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                        headers = {'Origin':'http://'+core.HOST,'spc-jwt-token':api.credentials['token']}
                        base = f'http://{core.HOST}/__book_ticket/{handoff.nonce}/'
                        events.append('ready')
                        conn.request('POST',base+'ready',headers=headers)
                        self.assertEqual(conn.getresponse().status,200); conn.close()
                        for _ in range(100):
                            conn = http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                            conn.request('POST',base+'claim',headers=headers)
                            response = conn.getresponse(); body=response.read(); conn.close()
                            if response.status == 200:
                                self.assertEqual(json.loads(body)['response']['main_order_no'],'order123')
                                break
                            threading.Event().wait(.01)
                        conn = http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                        conn.request('POST',base+'launched',headers=headers)
                        conn.getresponse().read();conn.close()
                    def checked_browser():
                        try: browser()
                        except BaseException as error: errors.append(error)
                    thread = threading.Thread(target=checked_browser)
                    holder['thread'] = thread
                    thread.start()
            venue = api.venue
            def guarded(name,body):
                if name == 'PayBooking':
                    self.assertTrue(holder['handoff'].ready.is_set())
                    events.append('post')
                return venue(name,body)
            with patch('common.system_proxy.proxy_session',side_effect=settings), \
                 patch.object(cashier,'Handoff',side_effect=create), \
                 patch('projects.dachanbay_tennis.capture_proxy.Proxy', side_effect=create_proxy), \
                 patch.object(core,'Api',return_value=api), patch.object(core,'DATA',Path(tmp)), \
                 patch.object(cashier,'check_frontend'), patch.object(api,'venue',side_effect=guarded):
                workflow.book_with_cashier(None,workflow.notice_digest(api.notices('tennis')),None,
                                           journal=journal,show=show,now=NOW,timeout=10)
            holder['thread'].join(5)
            self.assertEqual(errors,[])
            self.assertLess(events.index('ready'),events.index('post'))
            self.assertEqual(events[-1],'proxy-restored')
            self.assertEqual(journal.records[journal.key(TARGET)]['cashier_state'],'sdk_requested')
            self.assertEqual(sum(x[0]=='PayBooking' for x in api.calls),1)
            self.assertTrue((Path(tmp)/'credentials.dpapi').exists())

    @private_storage_test
    def test_fresh_session_requires_real_profile_validation_before_binding(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core,'DATA',Path(tmp)):
            handoff = cashier.Handoff(None,authorize=cashier.authorize_session)
            url=urlsplit(f'http://{core.HOST}/__book_ticket/{handoff.nonce}/ready')
            headers={'Origin':'http://'+core.HOST,'spc-jwt-token':'synthetic-fresh-token'}
            with patch.object(core.Api,'profile',side_effect=core.BookingError('expired')):
                self.assertEqual(handoff.handle('POST',url,headers,b'')[0],403)
            self.assertIsNone(handoff.credentials)
            self.assertFalse(handoff.ready.is_set())
            self.assertFalse((Path(tmp)/'credentials.dpapi').exists())
            with patch.object(core.Api,'profile',return_value={'require_verification':1}):
                self.assertEqual(handoff.handle('POST',url,headers,b'')[0],200)
            self.assertTrue(handoff.ready.is_set())
            self.assertNotIn(b'synthetic-fresh-token',(Path(tmp)/'credentials.dpapi').read_bytes())
            self.assertEqual(handoff.handle('POST',url,{**headers,'spc-jwt-token':'different-account'},b'')[0],403)


class ReleaseTests(unittest.TestCase):
    def test_clock_window_and_rollover(self):
        start,end = release_watch.window(NOW)
        self.assertEqual(start.strftime('%H:%M:%S'),'19:59:45')
        self.assertEqual(end.strftime('%H:%M:%S'),'20:03:00')
        self.assertEqual(release_watch.window(end)[0].date(),(end+dt.timedelta(days=1)).date())

    @private_storage_test
    def test_unavailable_then_release_single_submit_and_error_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = core.Journal(Path(tmp)/'journal.dpapi')
            api = BookingApi(journal)
            stop = threading.Event()
            missing = deepcopy(GRID)
            missing['items'][0]['booking_durations'][0]['status'] = 3
            with patch.object(api,'grid',side_effect=[missing,GRID,GRID,GRID]), \
                 patch.object(stop,'wait',return_value=False) as waits:
                result = release_watch.watch_release(api,workflow.notice_digest(api.notices('tennis')),None,
                                                     journal,stop,lambda s:None,clock=lambda:RELEASE_NOW)
            self.assertIsNotNone(result)
            waits.assert_called_once_with(.1)
            self.assertEqual(sum(x[0]=='PayBooking' for x in api.calls),1)
            with patch.object(api,'dates',side_effect=core.BookingError('HTTP 429')) as dates:
                with self.assertRaises(core.BookingError):
                    release_watch.watch_release(api,workflow.notice_digest(api.notices('tennis')),None,
                                                journal,stop,lambda s:None,clock=lambda:RELEASE_NOW)
                dates.assert_called_once()

    def test_only_new_release_date_not_an_older_target(self):
        journal = type('Journal',(),{'contains':lambda self,t:False})()
        api = BookingApi(journal)
        with (patch.object(api,'grid',side_effect=AssertionError('older dates must not be queried')),
             patch.object(api,'dates',return_value=[{'date':'2026-10-13'}]),
              patch.object(threading.Event,'wait',return_value=True)):
            # 10/13 at 20:00 releases 10/20, not the older available 10/13.
            result = release_watch.watch_release(api,workflow.notice_digest(api.notices('tennis')),None,
                                                 journal,threading.Event(),lambda s:None,
                                                 clock=lambda:RELEASE_NOW+dt.timedelta(days=7))
            self.assertIsNone(result)
