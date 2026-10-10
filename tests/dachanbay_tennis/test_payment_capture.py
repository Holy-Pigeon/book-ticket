import gzip
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from projects.dachanbay_tennis.payment_trace import Trace
from projects.dachanbay_tennis.capture_proxy import Proxy
from projects.dachanbay_tennis.core import HOST

RESPONSE={'main_order_no':'PRIVATE-ORDER-123','need_payment':True,'price':10000,
          'access_token':'PRIVATE-ACCESS','wx_openid':'PRIVATE-OPENID','session_id':'PRIVATE-SESSION',
          'url_params':'secret_signed_params','sign':'PRIVATE-SIGNATURE',
          'customer':{'phone':'PRIVATE-PHONE','name':'PRIVATE-NAME','user_id':123456789}}
RAW=gzip.compress(json.dumps(RESPONSE).encode())


class PaymentServer(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Encoding','gzip')
        self.send_header('Content-Length',str(len(RAW)))
        self.end_headers(); self.wfile.write(RAW)


class Tests(unittest.TestCase):
    def test_redaction_and_consistent_anonymous_order_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace=Trace(Path(tmp)/'trace.json')
            cleaned=trace.clean(RESPONSE)
            self.assertEqual(cleaned['price'],10000)
            self.assertTrue(cleaned['need_payment'])
            self.assertEqual(cleaned['main_order_no'],trace.clean('PRIVATE-ORDER-123','order_no'))
            other=Trace(Path(tmp)/'other.json')
            self.assertNotEqual(cleaned['main_order_no'],other.clean('PRIVATE-ORDER-123','order_no'))
            self.assertEqual(cleaned['url_params'],'<redacted>')
            self.assertNotIn('PRIVATE-',json.dumps(cleaned))
            self.assertNotIn('123456789',json.dumps(cleaned))
            self.assertNotIn('secret_signed_params',json.dumps(cleaned))

    def test_payment_scope_no_login_or_unrelated_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace=Trace(Path(tmp)/'trace.json')
            for path in ('/api/h5-basic-api/WxLogin','/api/h5-basic-api/GetInfoByUserId','/api/h5-venue-api/GetGroundNotice','/api/invalid/Pay?secret=bad'):
                trace.exchange('POST',path,b'{}',200,{},b'{}',2)
            self.assertEqual(trace.count(),0)
            trace.exchange('POST','/api/h5-fee-api/SuccessPayOrderUpdate',b'{"order_id":"PRIVATE-ORDER-123"}',200,{},b'{"code":0}',10)
            self.assertEqual(trace.count(),1)
            stored=trace.path.read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE-ORDER-123',stored)

    def test_compressed_truncated_and_invalid_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace=Trace(Path(tmp)/'trace.json')
            self.assertEqual(trace.body_shape(RAW,'gzip')['price'],10000)
            self.assertIn('_omitted',trace.body_shape(b'bad'))
            self.assertIn('_omitted',trace.body_shape(b'abc','br'))
            self.assertIn('_omitted',trace.body_shape(b'{}',complete=False))
            self.assertIn('_omitted',trace.body_shape(gzip.compress(b'a'*300000),'gzip'))

    def test_tunnel_metadata_only_and_host_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace=Trace(Path(tmp)/'trace.json')
            for host in ('example.com','api.weixin.qq.com','nottenpay.com','pay.qq.com.evil.invalid'):
                trace.tunnel(host,443,'connected',0,0)
            self.assertEqual(trace.count(),0)
            trace.tunnel('midas.gtimg.cn',443,'connected',0,0)
            trace.tunnel('wx.tenpay.com',443,'closed',123,456)
            self.assertEqual(trace.count(),2)
            self.assertFalse(trace.doc['tls_decrypted'])
            self.assertFalse(trace.doc['events'][1]['payload_captured'])

    def test_actual_proxy_preserves_payment_response_bytes_and_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace=Trace(Path(tmp)/'trace.json')
            upstream=ThreadingHTTPServer(('127.0.0.1',0),PaymentServer)
            threading.Thread(target=upstream.serve_forever,daemon=True).start()
            proxy=Proxy(lambda _:None,port=0,on_exchange=trace.exchange,on_tunnel=trace.tunnel)
            proxy.start(); original=http.client.HTTPConnection
            try:
                def mapped(host,port,**kw): return original('127.0.0.1',upstream.server_port,**kw)
                with patch('projects.dachanbay_tennis.capture_proxy.http.client.HTTPConnection',side_effect=mapped):
                    conn=original('127.0.0.1',proxy.server_port,timeout=5)
                    conn.request('POST',f'http://{HOST}/api/h5-venue-api/PayBooking',
                                 json.dumps({'scene':2,'mp_openid':'PRIVATE-REQUEST-OPENID','slots':[]}),
                                 {'spc-jwt-token':'PRIVATE-HEADER-TOKEN','Cookie':'PRIVATE-COOKIE'})
                    response=conn.getresponse()
                    self.assertEqual(response.status,200)
                    self.assertEqual(response.getheader('Content-Encoding'),'gzip')
                    self.assertEqual(response.read(),RAW)
                    conn.close()
                self.assertEqual(trace.count(),1)
                stored=trace.path.read_text(encoding='utf-8')
                self.assertNotIn('PRIVATE-',stored)
                self.assertNotIn('secret_signed_params',stored)
                self.assertEqual(trace.doc['events'][0]['response']['price'],10000)
            finally:
                proxy.stop(); upstream.shutdown(); upstream.server_close()

    def test_capture_failures_do_not_break_forwarding(self):
        upstream=ThreadingHTTPServer(('127.0.0.1',0),PaymentServer)
        threading.Thread(target=upstream.serve_forever,daemon=True).start()
        def fail(*args): raise OSError('simulated local write failure')
        proxy=Proxy(fail,port=0,on_exchange=fail); proxy.start()
        original=http.client.HTTPConnection
        try:
            def mapped(host,port,**kw): return original('127.0.0.1',upstream.server_port,**kw)
            with patch('projects.dachanbay_tennis.capture_proxy.http.client.HTTPConnection',side_effect=mapped):
                conn=original('127.0.0.1',proxy.server_port,timeout=5)
                conn.request('POST',f'http://{HOST}/api/h5-venue-api/PayBooking',b'{}',{'spc-jwt-token':'fake'})
                self.assertEqual(conn.getresponse().read(),RAW); conn.close()
        finally:
            proxy.stop(); upstream.shutdown(); upstream.server_close()


if __name__=='__main__': unittest.main(verbosity=2)
