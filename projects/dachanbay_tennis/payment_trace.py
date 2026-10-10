"""Opt-in, sanitized diagnostics of the user's own venue payment flow."""
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import threading
import zlib
from .core import DATA, TZ
from common.storage import write_plain_json as atomic_json

API_PATH = re.compile(r"^/api/[A-Za-z0-9_-]{1,64}/[A-Za-z][A-Za-z0-9_]{0,63}$")
PAYMENT_PATH = re.compile(r"pay|order|booking|payment|balance|wallet|recharge|contract", re.I)
SECRET_KEY = re.compile(r"token|cookie|password|passwd|pwd|secret|sign|ticket|nonce|openid|openkey|sessionid|session_id|url_params|prepay|phone|mobile|email|address|chinese_name|real_name|identity|bank|card_number|cert", re.I)
ORDER_KEY = re.compile(r"^(main_order_no|order_no|order_id|transaction_id)$", re.I)
PUBLIC_FIELDS = {"status", "code", "ret", "need_payment", "scene", "check_type", "is_violation",
                 "order_type", "type_id", "price", "amount", "total_fee", "total_amount", "fee",
                 "duration_fee_type", "consumption_rule", "booking_type", "remaining_count",
                 "date", "start_time", "end_time", "create_time", "pay_time", "expire_time",
                 "confirm_booking_agreement", "pf", "session_type", "sessiontype", "wx_appid", "appid"}
SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class Trace:
    def __init__(self, path=None, notify=None):
        self.path = path or DATA / ("payment-trace-" + dt.datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f") + ".json")
        self.notify = notify or (lambda _: None)
        self.salt = secrets.token_bytes(32)
        self.lock = threading.Lock()
        self.doc = {"format": 1, "scope": "venue payment JSON shape + opaque payment HTTPS tunnel metadata",
                    "tls_decrypted": False, "credentials_saved": False, "events": []}
        atomic_json(self.path, self.doc)

    def clean(self, value, key="", depth=0):
        if depth > 7:
            return "<depth limit>"
        if ORDER_KEY.fullmatch(key):
            if not isinstance(value, (str, int)):
                return "<order identifier>"
            digest = hmac.new(self.salt, str(value).encode(), hashlib.sha256).hexdigest()[:12]
            return "order#" + digest
        if SECRET_KEY.search(key):
            # Signed url_params are opaque: parsing them could preserve embedded credentials.
            return "<redacted>"
        if isinstance(value, dict):
            return {(k if SAFE_FIELD.fullmatch(str(k)) else "_unrecognized_field"): self.clean(v, str(k), depth+1)
                    for k, v in list(value.items())[:100]}
        if isinstance(value, list):
            return {"_count": len(value), "_sample": [self.clean(v, "", depth+1) for v in value[:2]]}
        if value is None or isinstance(value, bool):
            return value
        if key in PUBLIC_FIELDS:
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, str):
                # Retain only dates, times, numbers and public enum/application identifiers.
                if re.fullmatch(r"[0-9T: .+/-]{1,40}", value):
                    return value
                if key in {"pf", "session_type", "sessiontype", "wx_appid", "appid"} and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                    return value
        if isinstance(value, str):
            return f"<string length={len(value)}>"
        return "<number>" if isinstance(value, (int, float)) else "<value>"

    def body_shape(self, raw, encoding="", complete=True):
        if not complete or len(raw) > 262144:
            return {"_omitted": "body exceeds capture limit"}
        try:
            if encoding.lower() == "gzip":
                # Bounded decompression: do not expand arbitrary large responses in memory.
                obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw = obj.decompress(raw, 262145)
                if len(raw) > 262144 or not obj.eof:
                    return {"_omitted": "expanded body exceeds capture limit"}
            elif encoding.lower() == "deflate":
                obj = zlib.decompressobj()
                raw = obj.decompress(raw, 262145)
                if len(raw) > 262144 or not obj.eof:
                    return {"_omitted": "expanded body exceeds capture limit"}
            elif encoding and encoding.lower() != "identity":
                return {"_omitted": "unsupported content encoding"}
            return self.clean(json.loads(raw))
        except Exception:
            return {"_omitted": "not valid JSON"}

    def append(self, event):
        with self.lock:
            if len(self.doc["events"]) >= 500:
                return
            event["time"] = dt.datetime.now(TZ).isoformat()
            self.doc["events"].append(event)
            atomic_json(self.path, self.doc)

    def exchange(self, method, path, request, status, headers, response, response_size):
        if not API_PATH.fullmatch(path) or not PAYMENT_PATH.search(path.rsplit("/",1)[-1]):
            return
        headers = {k.lower():v for k,v in headers.items()}
        code = headers.get("trpc-func-ret") or headers.get("trpc-ret") or "0"
        if not re.fullmatch(r"-?\d{1,12}", code):
            code = "<non-numeric>"
        event = {"kind": "venue_api", "method": method, "path": path, "http_status": status,
                 "business_code": code, "business_error_present": bool(headers.get("trpc-error-msg")),
                 "request": self.body_shape(request),
                 "response": self.body_shape(response, headers.get("content-encoding", ""), response_size <= 262144),
                 "response_bytes": response_size}
        self.append(event)
        self.notify(f"已采集 {path.rsplit('/',1)[-1]}（HTTP {status}，业务码 {code}）；参数已脱敏。")

    def tunnel(self, host, port, phase, sent, received):
        host = host.lower().rstrip(".")
        # Only public payment service hostnames; no general browsing or WeChat chat hosts.
        allowed = host == "midas.gtimg.cn" or any(host == s or host.endswith("." + s)
                   for s in ("pay.qq.com", "tenpay.com", "wechatpay.cn", "pay.weixin.qq.com"))
        if not allowed or not re.fullmatch(r"[a-z0-9.-]{1,253}", host):
            return
        self.append({"kind":"opaque_https_tunnel", "host":host, "port":port,
                     "phase":phase, "client_bytes":sent, "server_bytes":received,
                     "payload_captured":False})
        if phase == "connected":
            self.notify(f"支付 HTTPS 连接：{host}（仅记录域名，未解密）。")

    def count(self):
        with self.lock:
            return len(self.doc["events"])
