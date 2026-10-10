"""Initialize this project's local preferences after capturing an own-account session."""
import hashlib
from html.parser import HTMLParser
from urllib.parse import unquote
from . import core, site_crypto
from .api import Api
from .booking import money, write_private


class NoticeText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        elif tag in {'br', 'p', 'div', 'li'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def configure(service=None, fee_cap=None, ask=input, show=print):
    api = Api()
    api.verify()
    cards = api.cards()
    if len(cards) != 1 or not cards[0].get('available'):
        raise core.LocalError('当前版本要求唯一可用就诊人，请在微信核对；未初始化。')
    card = cards[0]
    patient = {k: site_crypto.decrypt_field(card.get(k, ''))
               for k in ['healthCardNo', 'patName', 'patIdno', 'phoneNo']}
    if not all(patient.values()):
        raise core.LocalError('就诊人必要资料不完整，请在微信补全。')
    data = api.schedules()
    target = unquote(api.params['deptName'])
    selected = next(((d, r) for d in data['regInfos'] for r in d.get('scheduleInfos') or []
                     if r.get('deptName') == target and r.get('deptId')), None)
    if not selected:
        raise core.LocalError('当前没有目标科室排班，无法核对告知内容；请稍后重新初始化。')
    doc, row = selected
    flow = api.request('POST', '/gateway/registration/support/beforeRegister/window', body={
        'birthday': card.get('birthday', ''), 'deptId': row['deptId'], 'doctorId': doc['doctorId'],
        'healthCardNo': patient['healthCardNo'], 'idCardNo': patient['patIdno'], 'patientId': card['patId'],
        'patientName': patient['patName'], 'phone': patient['phoneNo'],
        'regDate': row['regDate'], 'shiftName': row['shiftName']})
    if (not isinstance(flow, dict) or flow.get('title') != '剩余生物样本捐赠知情同意书'
            or str(flow.get('type')) != '2' or str(flow.get('needSignature', '')) == '1'
            or flow.get('btnName1') != '不同意' or flow.get('btnName2') != '同意'
            or not isinstance(flow.get('content'), str) or not flow['content']):
        raise core.LocalError('告知内容需要人工核对或签名，请在微信处理；未保存选择。')
    notice = NoticeText()
    notice.feed(flow['content'])
    show(flow['title'] + '\n' + ''.join(notice.parts).strip())
    show('捐赠是独立选择，拒绝不影响正常诊疗。程序只保存本机选择，不在此步骤挂号。')
    choice = ask('阅读上方当前告知后，请输入“同意”或“不同意”：').strip()
    if choice not in {'同意', '不同意'}:
        raise core.LocalError('没有明确捐赠选择，未保存。')
    service = service or ask('服务对象，请输入“医保”或“自费”：').strip()
    if service not in {'医保', '自费'}:
        raise core.LocalError('服务对象无效，未保存。')
    cap = money(fee_cap if fee_cap is not None else ask('单次挂号费用上限（元）：').strip())
    preferences = {'service': service, 'donation': 'agree' if choice == '同意' else 'decline',
                   'fee_cap': str(cap), 'consent_hash': hashlib.sha256(flow['content'].encode('utf-8')).hexdigest()}
    write_private('preferences.dpapi', preferences)
    show(f'已保存本机设置：{service}，费用上限 {cap} 元；不自动付款，未提交预约。')
    return preferences
