"""Import an own-account HAR locally and replay only verified read endpoints."""
import http.client
import json
import os
from pathlib import Path
import ssl
from common.errors import LocalError
from common.storage import protect, project_data_dir
from urllib.parse import parse_qs, unquote, urlsplit

HOST = 'mp.mhealth100.com'
HOSPITAL = '94CC9389F3E1486480C3EAC43F20CC49'
SCHEDULE = '/gateway/registration/appointment/scheduleNew/find'
IDENTITY = '/gateway/system/hospital/teantId/find'
DATA = project_data_dir('pkuszh')
ALLOWED_HEADERS = {'cookie', 'token', 'fronthospitalcode', 'user-agent'}


def validate_url(url):
    try:
        u = urlsplit(url)
        if (u.scheme != 'https' or u.hostname != HOST or u.port not in (None, 443)
                or u.username or u.password or u.fragment or u.path != SCHEDULE):
            raise ValueError()
        q = parse_qs(u.query, keep_blank_values=True, strict_parsing=True)
        # The frontend percent-encodes deptName before axios encodes it again.
        name = unquote(unquote(q.get('deptName', [''])[0]))
        if '牙体牙髓' not in name or not q.get('deptId', [''])[0]:
            raise ValueError()
        if any(len(v) != 1 for v in q.values()):
            raise ValueError()
        if set(q) - {'branchCode', 'deptId', 'deptName', 'deptType',
                     'startDate', 'endDate', 'ajaxConfig'}:
            raise ValueError()
        if any(c in url for c in '\r\n'):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise LocalError('请求不是目标域名的牙体牙髓科排班查询，已拒绝导入。') from None
    return u.path + '?' + u.query


def select_request(har):
    entries = har.get('log', {}).get('entries', [])
    candidates = []
    for entry in entries:
        req = entry.get('request', {})
        if req.get('method') != 'GET':
            continue
        url = req.get('url', '')
        try:
            validate_url(url)
        except LocalError:
            continue
        headers = {}
        for h in req.get('headers', []):
            k, v = str(h.get('name', '')).lower(), str(h.get('value', ''))
            if k in ALLOWED_HEADERS:
                if '\r' in v or '\n' in v or len(v) > 65536:
                    raise LocalError('请求头格式异常，未导入。')
                headers[k] = v
        if not headers.get('cookie'):
            cookies = req.get('cookies', [])
            if cookies:
                headers['cookie'] = '; '.join(str(x['name']) + '=' + str(x['value']) for x in cookies)
        if any('\r' in v or '\n' in v for v in headers.values()):
            raise LocalError('请求头格式异常，未导入。')
        if headers.get('cookie') or headers.get('token'):
            candidates.append({'url': url, 'headers': headers})
    if not candidates:
        raise LocalError('未找到带登录态的牙体牙髓科排班 GET 请求。请刷新目标科室，并在本机导出包含敏感数据的 HAR；文件无需发送给任何人。')
    return candidates[-1]


def import_har(path):
    p = Path(path)
    if p.stat().st_size > 100 * 1024 * 1024:
        raise LocalError('HAR 超过 100 MB，请清空网络记录后只刷新目标科室再导出。')
    try:
        value = select_request(json.loads(p.read_text(encoding='utf-8-sig')))
    except LocalError:
        raise
    except Exception:
        raise LocalError('HAR 无法解析，未保存。') from None
    save_session(value)
    return '已在当前 Windows 账户下加密保存排班请求的登录态。未保存 HAR 内的其他请求或响应。'


def save_session(value):
    validate_url(value['url'])
    if set(value['headers']) - ALLOWED_HEADERS:
        raise LocalError('请求头不在允许范围内。')
    if any('\r' in str(v) or '\n' in str(v) for v in value['headers'].values()):
        raise LocalError('请求头格式异常。')
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / 'session.tmp'
    with tmp.open('wb') as f:
        f.write(protect(json.dumps(value).encode('utf-8')))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA / 'session.dpapi')


def load_session():
    try:
        value = json.loads(protect((DATA / 'session.dpapi').read_bytes(), True))
        validate_url(value['url'])
        if set(value['headers']) - ALLOWED_HEADERS:
            raise ValueError()
        if any('\r' in str(v) or '\n' in str(v) for v in value['headers'].values()):
            raise ValueError()
        return value
    except Exception:
        raise LocalError('尚未导入有效登录态，或登录态文件无法读取，请重新导入。') from None


def get_json(target, headers):
    if target != IDENTITY:
        validate_url('https://' + HOST + target)
    # No environment proxy, redirects, retries, or TLS verification exceptions.
    conn = http.client.HTTPSConnection(HOST, timeout=25, context=ssl.create_default_context())
    try:
        h = {k: v for k, v in headers.items() if k in ALLOWED_HEADERS}
        h.update({'Accept': 'application/json', 'Referer': 'https://' + HOST + '/patient/registration/'})
        conn.request('GET', target, headers=h)
        r = conn.getresponse()
        raw = r.read(8 * 1024 * 1024 + 1)
        if r.status == 401:
            raise LocalError('登录态无效或已过期，请在正常登录的浏览器重新采集。')
        if r.status != 200:
            raise LocalError(f'查询返回 HTTP {r.status}，已停止，未重试。')
        if len(raw) > 8 * 1024 * 1024:
            raise LocalError('响应过大，已停止。')
        result = json.loads(raw)
        if not isinstance(result, dict) or str(result.get('resultCode')) != '0':
            raise LocalError('接口未返回成功结果，请在原预约页面查看提示；未输出原始响应。')
        return result
    except LocalError:
        raise
    except Exception:
        raise LocalError('请求失败或响应格式不符，已停止，未重试。') from None
    finally:
        conn.close()


def summarize(result):
    data = result.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('regInfos'), list):
        raise LocalError('排班结构与公开前端不符，需核对后继续。')
    lines, available, total = [], 0, 0
    for doc in data['regInfos']:
        schedules = doc.get('scheduleInfos') or []
        if not isinstance(schedules, list):
            raise LocalError('医生排班结构异常。')
        for row in schedules:
            total += 1
            count = row.get('regLeaveCount')
            try:
                usable = float(count) > 0 and str(row.get('regStatus')) == '1'
            except (ValueError, TypeError):
                usable = False
            available += int(usable)
            status = ('可约，剩余 ' + str(count)) if usable else '当前不可直接预约'
            lines.append(f"{doc.get('doctorName', '')}  {row.get('regDate', '')}  {row.get('shiftName', '')}  {status}")
    return f'已查询 {len(data["regInfos"])} 位医生、{total} 条排班，其中 {available} 条可直接预约。\n' + '\n'.join(lines)


def query_once():
    session = load_session()
    identity = get_json(IDENTITY, session['headers']).get('data')
    if not isinstance(identity, dict) or identity.get('hospitalCode') != HOSPITAL:
        raise LocalError('登录会话未确认为北京大学深圳医院，已停止查询。')
    result = get_json(validate_url(session['url']), session['headers'])
    return summarize(result)
