"""One-shot handoff to the venue's real cashier, in the user's WeChat session.

No charge, cancellation, or success-update API is called here. The public frontend
version is pinned: changes stop the workflow BEFORE creating a reservation.
"""
import hashlib
import hmac
import http.client
import json
import secrets
import threading
import time
from urllib.parse import parse_qs

from . import core

ASSET = '/assets/index-D4e9KijV.js'
ASSET_SHA256 = 'd0558a33008ed5b7d4948baef2dc1ffcdd206a777578650a5ebaefc2d4c60ad3'


def check_frontend():
    conn = http.client.HTTPConnection(core.HOST, timeout=20)
    try:
        conn.request('GET', '/index')
        response = conn.getresponse()
        html = response.read(512 * 1024)
        if response.status != 200 or ASSET.encode() not in html:
            raise core.BookingError('官网前端版本已变化，未下单；请更新收银台适配。')
        conn.close()
        conn = http.client.HTTPConnection(core.HOST, timeout=20)
        conn.request('GET', ASSET)
        response = conn.getresponse()
        script = response.read(4 * 1024 * 1024 + 1)
        if response.status != 200 or hashlib.sha256(script).hexdigest() != ASSET_SHA256:
            raise core.BookingError('官网收银台版本校验未通过，未下单。')
    except core.BookingError:
        raise
    except Exception:
        raise core.BookingError('无法校验官网收银台，未下单。') from None
    finally:
        conn.close()


def page(nonce, timeout=180):
    # No credential or signed payment parameters are embedded in HTML or URLs.
    script = r'''
import {Z as cashier, aw as profile, A as user} from "__ASSET__";
const base = "/__book_ticket/__NONCE__/";
const banner = document.createElement("div");
banner.style = "position:fixed;z-index:2147483647;top:0;left:0;right:0;background:#e8ffbf;padding:24px;color:#172219;font:20px sans-serif";
document.body.append(banner);
function status(text) { banner.textContent = text; }
async function request(action, token) {
  return fetch(base + action, {method:"POST", cache:"no-store", credentials:"same-origin",
    headers:{"spc-jwt-token":token,"spc-mp-openid":user().mpOpenId || ""}});
}
try {
  const deadline = Date.now() + __TIMEOUT__ * 1000;
  let token = "", attempted = "";
  while (Date.now() < deadline) {
    try { token = await profile().getJwt({autoRenew:false}); } catch (_) { token = ""; }
    if (token && token !== attempted) {
      attempted = token;
      const ready = await request("ready", token);
      if (ready.ok) break;
      if (ready.status === 410) throw Error();
    }
    status("请在微信完成官网登录授权，助手会自动继续。当前尚未下单。");
    await new Promise(r=>setTimeout(r,1000));
    token = "";
  }
  if (!token || Date.now() >= deadline) throw Error();
  status("微信已就绪，助手将按设定时间检查并提交预约。请保持本页打开，最后付款由你确认。");
  let packet;
  for (let i=0; i<1800; i++) {
    const response = await request("claim", token);
    if (response.status === 202) { await new Promise(r=>setTimeout(r,1000)); continue; }
    if (!response.ok) throw Error();
    packet = await response.json(); break;
  }
  if (!packet) throw Error();
  if (packet.error) { status(packet.error); await request("launched", token); }
  else if (!packet.response.need_payment) {
    status("预约接口已返回免支付结果，请在微信核对预约。");
    await request("launched", token);
  } else {
    user().setMpOpenid(packet.mp_openid || "");
    const booking = packet.booking;
    const detail = booking ? `${booking.date} ${booking.start}–${booking.end}，${booking.ground_name}，¥${(booking.price_cents/100).toFixed(2)}。` : "";
    status(detail + "预约接口已返回，请点击继续付款。最终金额以官网收银台为准。");
    const button = document.createElement("button");
    button.textContent = "继续付款";
    button.style = "display:block;margin-top:16px;padding:12px 24px;font:20px sans-serif;cursor:pointer";
    banner.append(button);
    const success = "/venue_success?main_order_no=" + encodeURIComponent(packet.response.main_order_no);
    const router = {push(value) {
      const target = typeof value === "string" ? value : value.path + "?" + new URLSearchParams(value.query || {});
      if (target.startsWith("/venue_success?")) window.location.assign(target);
    }};
    // The real SDK owns payment confirmation and the success callback.
    // Omit its onClose callback, which the original booking page uses to cancel.
    // Invoke directly inside the user's click handler, preserving native user activation.
    // Automatic polling never launches the payment SDK.
    let started = false;
    await new Promise((resolve,reject)=>{
      button.onclick = async()=>{
        if (started) return;
        started = true;
        button.disabled = true;
        if (Date.now() >= deadline) {
          status("付款交接已过期，请在微信核对这笔订单，勿重新下单。");
          resolve(); return;
        }
        try {
          status("正在调起官网收银台；付款请由本人确认。关闭收银台不会自动取消或重订。");
          await cashier(packet.response, {route:{path:"/index",query:{}},router}, success, "/index");
          await request("launched", token);
          banner.remove(); resolve();
        } catch (error) { reject(error); }
      };
    });
  }
} catch (_) { status("收银台交接失败或已过期。请回助手查看结果，勿重复下单；需要时在微信核对或联系场馆。"); }
'''
    script = script.replace('__ASSET__', ASSET).replace('__NONCE__', nonce).replace('__TIMEOUT__',str(max(1,int(timeout))))
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>昀湾预约 · 本人付款</title></head><body><div id="app"></div>'
            '<script src="https://res.wx.qq.com/open/js/jweixin-1.6.0.js"></script>'
            '<script type="module">' + script + '</script></body></html>').encode('utf-8')


class Handoff:
    def __init__(self, credentials, timeout=180, authorize=None):
        self.nonce = secrets.token_urlsafe(24)
        self.credentials = credentials
        self.authorize = authorize
        self.deadline = time.monotonic() + timeout
        self.last_seen = time.monotonic()
        self.ready, self.launched = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.packet = None
        self.delivered = False
        self.url = f'http://{core.HOST}/index?book_ticket_handoff={self.nonce}'

    def publish(self, result):
        with self.lock:
            if self.delivered or self.packet is not None:
                raise core.BookingError('已有收银台交接结果，禁止覆盖或重复下单。')
            self.packet = dict(response=result['response'], mp_openid=self.credentials.get('mp_openid', ''))
            if result.get('target'):
                date, start, end = result['target']
                self.packet['booking'] = dict(date=date, start=start, end=end,
                    ground_name=result['ground_name'], price_cents=result['price_cents'])

    def fail(self, message):
        with self.lock:
            if self.packet is None and not self.delivered:
                self.packet = {'error': message}

    def handle(self, method, url, headers, body):
        prefix = '/__book_ticket/'
        if url.hostname != core.HOST or (url.port or 80) != 80:
            return None
        entry = url.path == '/index' and 'book_ticket_handoff' in parse_qs(url.query)
        if not entry and not url.path.startswith(prefix):
            return None
        def answer(code, value):
            return code, 'application/json; charset=utf-8', json.dumps(value).encode()
        if time.monotonic() >= self.deadline:
            return answer(410, {'error': 'expired'})
        if entry:
            if method != 'GET' or parse_qs(url.query).get('book_ticket_handoff') != [self.nonce]:
                return answer(404, {})
            return 200, 'text/html; charset=utf-8', page(self.nonce,self.deadline-time.monotonic())
        expected = prefix + self.nonce + '/'
        if not url.path.startswith(expected) or method != 'POST' or body:
            return answer(404, {})
        if headers.get('Origin') != f'http://{core.HOST}':
            return answer(403, {})
        action = url.path[len(expected):]
        with self.lock:
            token = headers.get('spc-jwt-token', '').strip()
            if not token:
                return answer(403, {})
            if self.credentials is None:
                if action != 'ready' or self.authorize is None:
                    return answer(403, {})
                candidate = {'token':token, 'cookie':headers.get('Cookie',''),
                             'user_agent':headers.get('User-Agent',''),
                             'mp_openid':headers.get('spc-mp-openid','')}
                try:
                    self.authorize(candidate)  # Validate with the REAL user API first.
                except Exception:
                    return answer(403, {})
                self.credentials = candidate
            if not hmac.compare_digest(token.encode(), self.credentials['token'].encode()):
                return answer(403, {})
            if action in ('ready','claim','launched'):
                self.last_seen = time.monotonic()
            if action == 'ready':
                self.ready.set()
                return answer(200, {})
            if action == 'claim':
                if not self.ready.is_set() or self.delivered:
                    return answer(409, {})
                if self.packet is None:
                    return answer(202, {})
                packet, self.packet = self.packet, None
                self.delivered = True  # Even an interrupted response must never replay.
                return answer(200, packet)
            if action == 'launched' and self.delivered:
                self.launched.set()
                return answer(200, {})
        return answer(404, {})


def authorize_session(value):
    core.Api(value).profile()
    core.save_credentials(value)


def check_receiver(api=None, *, timeout=180, manual=False, service=None, show=print):
    """Live browser/login handshake only. This function never calls a booking API."""
    from common.system_proxy import proxy_session
    from .capture_proxy import Proxy
    if api is not None:
        api.profile()
    check_frontend()
    handoff = Handoff(api.credentials if api else None, timeout,
                      authorize=authorize_session if api is None else None)
    try:
        proxy = Proxy(lambda value:None, local_handler=handoff.handle)
    except OSError:
        raise core.BookingError('端口 8766 被占用，无法验证微信页面。') from None
    proxy.start()
    def wait(event):
        while not event.wait(.25):
            if time.monotonic() >= handoff.deadline:
                raise core.BookingError('微信页面验证超时，没有下单。')
    try:
        with proxy_session(core.DATA/'proxy-recovery.dpapi',manual,service,show):
            show('请在电脑微信打开以下链接；本次仅验证官网模块与登录态，不创建订单：')
            show(handoff.url)
            wait(handoff.ready)
            handoff.fail('微信登录态握手和官网模块加载验证成功。本次没有下单，也没有发起付款。')
            wait(handoff.launched)
            show('微信页面已确认收到验证结果；真实收银台付款仍待实际订单验证。')
    finally:
        proxy.stop()
