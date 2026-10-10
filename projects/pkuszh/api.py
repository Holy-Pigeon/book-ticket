"""Hospital own-session API. Read/configuration calls only during setup."""
import datetime as dt
import http.client
import json
import ssl
from urllib.parse import urlencode, urlsplit, parse_qsl
from . import core


class DateRangeError(core.LocalError):
    pass


STAGE_NAMES = {
    core.SCHEDULE: '医生排班查询',
    '/gateway/registration/doctor/time/find': '分时号源查询',
    '/gateway/registration/support/beforeRegister/window': '挂号前告知查询',
    '/gateway/registration/svobject/find': '医保服务对象查询',
    '/gateway/registration/cost/find': '挂号费用核对',
    '/gateway/registration/history/judge': '重复预约核对',
    '/gateway/registration/current/find': '已有挂号查询',
    '/gateway/registration/unPaidOrder/judge': '待支付订单查询',
    '/gateway/patient/healthcard/findOnlineInsuranceStatus': '在线医保状态查询',
}


class BusinessError(core.LocalError):
    def __init__(self, path, code, reason=''):
        self.path, self.code = path, str(code)
        self.private_reason = reason
        # Never expose arbitrary server messages: they can contain patient fields.
        safe_reasons = {'未知系统异常', '号源不足', '号源已满', '已约满', '无可预约号源', '参数错误'}
        self.reason = reason if reason in safe_reasons else ''
        label = STAGE_NAMES.get(path, '医院接口核对')
        detail = ('；' + self.reason) if self.reason else ''
        super().__init__(f'{label}未成功（业务码 {self.code[:20]}{detail}），未提交预约。')


class RateLimitError(core.LocalError):
    def __init__(self, retry_after=30):
        self.retry_after = retry_after
        super().__init__('医院接口限流，请稍后再试。')


class TransientRequestError(core.LocalError):
    def __init__(self, path, kind):
        self.path, self.kind = path, kind
        label = STAGE_NAMES.get(path, '医院接口查询')
        super().__init__(f'{label}暂时失败（{kind}）。')


class ResponseFormatError(core.LocalError):
    def __init__(self, path, kind):
        self.path, self.kind = path, kind
        label = STAGE_NAMES.get(path, '医院接口查询')
        super().__init__(f'{label}响应异常（{kind}），需核对后再运行。')

TZ = dt.timezone(dt.timedelta(hours=8))
GET_PATHS = {core.IDENTITY, core.SCHEDULE,
    '/gateway/system/hospital/branchList/find',
    '/gateway/patient/healthcard/api/findCards',
    '/gateway/patient/healthcard/findOnlineInsuranceStatus',
    '/gateway/registration/doctor/time/find',
    '/gateway/registration/svobject/find',
    '/gateway/registration/cost/find',
    '/gateway/registration/unPaidOrder/judge',
    '/gateway/registration/appointment/schedule/isRegistered',
    '/gateway/registration/current/find',
    '/gateway/registration/register/limit/info',
    '/gateway/system/config/find'}
POST_PATHS = {'/gateway/system/page/config', '/gateway/registration/history/judge',
              '/gateway/registration/support/beforeRegister/window'}


class Api:
    def __init__(self):
        self.session = core.load_session()
        self.params = dict(parse_qsl(urlsplit(self.session['url']).query, keep_blank_values=True))

    def request(self, method, path, params=None, body=None):
        if method == 'GET' and path not in GET_PATHS or method == 'POST' and path not in POST_PATHS or method not in {'GET', 'POST'}:
            raise core.LocalError('接口或方法不在已核对的允许范围内。')
        target = path + (('?' + urlencode(params)) if params else '')
        headers = dict(self.session['headers'])
        headers.update({'Accept': 'application/json', 'Referer': 'https://' + core.HOST + '/patient/registration/',
                        'Origin': 'https://' + core.HOST})
        raw_body = None
        if method == 'POST':
            headers['Content-Type'] = 'application/json;charset=UTF-8'
            raw_body = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        conn = http.client.HTTPSConnection(core.HOST, timeout=25, context=ssl.create_default_context())
        try:
            conn.request(method, target, raw_body, headers)
            r = conn.getresponse()
            raw = r.read(8 * 1024 * 1024 + 1)
            if r.status == 401:
                raise core.LocalError('医院登录态已过期，已停止。请在微信正常登录后重新采集。')
            if r.status == 429:
                try:
                    delay = min(3600, max(1, int(r.getheader('Retry-After', '30'))))
                except ValueError:
                    delay = 30
                raise RateLimitError(delay)
            if r.status in {502, 503, 504}:
                raise TransientRequestError(path, f'HTTP {r.status}')
            if r.status != 200:
                raise core.LocalError(f'医院接口返回 HTTP {r.status}，未重试。')
            if len(raw) > 8 * 1024 * 1024:
                raise core.LocalError('医院响应过大，已停止。')
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ResponseFormatError(path, '非有效 JSON') from None
            if isinstance(value, dict) and value.get('resultDesc') == '无效的日期范围':
                raise DateRangeError('医院尚不接受这个日期范围。')
            if not isinstance(value, dict) or str(value.get('resultCode')) != '0':
                raise BusinessError(path, value.get('resultCode') if isinstance(value, dict) else 'invalid',
                                    value.get('resultDesc', '') if isinstance(value, dict) else '')
            return value.get('data')
        except core.LocalError:
            raise
        except ssl.SSLError:
            raise core.LocalError('TLS 证书或加密连接校验失败，已停止，请检查网络环境。') from None
        except TimeoutError:
            raise TransientRequestError(path, '网络超时') from None
        except (http.client.RemoteDisconnected, http.client.IncompleteRead, ConnectionError):
            raise TransientRequestError(path, '连接中断') from None
        except OSError:
            raise TransientRequestError(path, '网络连接失败') from None
        except http.client.HTTPException:
            raise ResponseFormatError(path, 'HTTP 协议异常') from None
        except Exception:
            raise core.LocalError('本机接口处理异常，已停止；未记录原始异常以保护隐私。') from None
        finally:
            conn.close()

    def verify(self):
        data = self.request('GET', core.IDENTITY)
        if not isinstance(data, dict) or data.get('hospitalCode') != core.HOSPITAL:
            raise core.LocalError('会话未确认为北京大学深圳医院，已停止。')
        self.identity = data

    def schedules(self):
        now = dt.datetime.now(TZ)
        params = dict(self.params)
        params['startDate'] = (now.date() + dt.timedelta(days=1)).isoformat()
        # Seven days ahead is the displayed hospital booking limit. Server enforces release.
        days = 7 if now.hour >= 17 else 6
        params['endDate'] = (now.date() + dt.timedelta(days=days)).isoformat()
        try:
            data = self.request('GET', core.SCHEDULE, params)
        except DateRangeError:
            if days != 7:
                raise
            params['endDate'] = (now.date() + dt.timedelta(days=6)).isoformat()
            data = self.request('GET', core.SCHEDULE, params)
        if not isinstance(data, dict) or not isinstance(data.get('regInfos'), list):
            raise core.LocalError('排班响应结构需要核对。')
        return data

    def cards(self):
        data = self.request('GET', '/gateway/patient/healthcard/api/findCards')
        if not isinstance(data, list):
            raise core.LocalError('就诊人列表结构需要核对。')
        return data

    def configs(self, fields):
        data = self.request('POST', '/gateway/system/page/config',
                            body={'configRequestList': fields})
        if not isinstance(data, dict) or not isinstance(data.get('configRequestList'), list):
            raise core.LocalError('预约配置结构需要核对。')
        return data['configRequestList']
