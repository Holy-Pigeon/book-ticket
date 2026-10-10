"""Normal appointment submission. One durable submission attempt, no payment calls."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import json
import secrets
import ssl
import time
from pathlib import Path
from common.storage import write_json, read_json
from common.concurrency import SingleInstance
from urllib.parse import quote, unquote
from . import core
from .api import Api, TZ
from . import site_crypto

ORDER_PATH = '/gateway/registration/appointment/order/create'
INSURANCE_ORDER_PATH = '/gateway/registration/appointment/order/insurance/create'


def write_private(name, value):
    write_json(core.DATA / name, value)


def read_private(name):
    return read_json(core.DATA / name)


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise InvalidOperation()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise core.LocalError('费用字段异常，未提交。') from None


def available(row):
    try:
        return (Decimal(str(row.get('regLeaveCount', '0'))) > 0
                and str(row.get('regStatus')) == '1'
                and str(row.get('allowExtraAppoint', '')) != '1'
                and str(row.get('pinpointLock', '')) != '1')
    except (InvalidOperation, ValueError):
        return False


def same_dept(order, dept):
    return (str(order.get('deptId', '')) == str(dept)
            or '牙体牙髓' in str(order.get('deptName', '')))


def Mutex():
    return SingleInstance('pkuszh-booking-01647')


class Booker:
    def __init__(self, api=None):
        self.api = api or Api()
        self.settings = read_private('preferences.dpapi')
        if self.settings.get('service') not in {'医保', '自费'}:
            raise core.LocalError('尚未选择医保或自费。')
        if self.settings.get('donation') not in {'agree', 'decline'}:
            raise core.LocalError('尚未选择生物样本捐赠意愿。')
        self.cap = money(self.settings.get('fee_cap', '100'))
        self.cooldowns = {}

    @staticmethod
    def schedule_key(doc, row):
        return tuple(str(v) for v in (doc.get('doctorId'), row.get('deptId'), row.get('scheduleId'),
                                     row.get('regDate'), row.get('timeFlag')))

    def defer(self, doc, row, delay):
        if not hasattr(self, 'cooldowns'):
            self.cooldowns = {}
        self.cooldowns[self.schedule_key(doc, row)] = time.monotonic() + delay

    def setup(self):
        self.api.verify()
        self.cards = self.api.cards()
        if len(self.cards) != 1 or not self.cards[0].get('available'):
            raise core.LocalError('就诊人数量或状态发生变化，需在本机重新选择。')
        self.card = self.cards[0]
        self.patient = {k: site_crypto.decrypt_field(self.card.get(k, ''))
                        for k in ['healthCardNo', 'patName', 'patIdno', 'phoneNo']}
        if not all(self.patient.values()):
            raise core.LocalError('就诊人必要资料不完整，请在微信补全。')
        branches = self.api.request('GET', '/gateway/system/hospital/branchList/find', {'productType': 0})
        matches = [b for b in branches if b.get('branchCode') == self.api.params.get('branchCode')]
        if len(matches) != 1:
            raise core.LocalError('院区不明确，未提交。')
        self.branch = matches[0]
        fields = json.loads(Path(__file__).with_name('config_fields.json').read_text(encoding='utf-8'))
        self.configs = self.api.configs([{'dataType': c['dataType'], 'mateName': c['mateName']} for c in fields])
        if self.enabled('verification.code') or self.enabled('registration.face.nuclear.body') or self.enabled('timeSharing.customAuthInfo'):
            raise core.LocalError('医院当前要求滑块、人脸或其他人工授权，请回微信完成；脚本已停止。')
        if self.settings['service'] == '医保' and not (self.enabled('BESPOKE_REPORT') and self.enabled('country.insurance.no.verify')):
            raise core.LocalError('医保当前需要单独授权，请在微信完成；未提交。')
        online = self.api.request('GET', '/gateway/patient/healthcard/findOnlineInsuranceStatus')
        if not isinstance(online, dict) or 'open' not in online:
            raise core.LocalError('在线医保状态结构需要核对，未提交。')
        self.online_insurance = online['open'] is True or str(online['open']) == '1'
        city = self.api.request('GET', '/gateway/system/config/find',
            {'dataType': 'WEIXIN_ONLINE_MEDICARE_CONFIG', 'metaName': 'weixin.insurance.cityId'})
        if not isinstance(city, list):
            raise core.LocalError('院区医保配置结构需要核对，未提交。')
        self.insurance_city = str(city[0].get('value', '')) if city else ''
        self.check_existing()

    def enabled(self, name, data_type='HOSPITAL_ORDER_CONFIG'):
        return any(c['dataType'] == data_type and c['mateName'] == name
                   and str(c.get('value')) == '1' for c in self.configs)

    def country_insurance_cost_not_needed(self, service):
        def value(data_type, name):
            return next((c.get('value') for c in self.configs
                         if c['dataType'] == data_type and c['mateName'] == name), None)
        # Mirrors selectServiceObj/openCountryInsurance in the published client.
        # This is a separate normal branch, never a fallback from a failed check.
        return (self.settings['service'] == '医保'
                and 'micromessenger' in getattr(self.api, 'session', {}).get('headers', {}).get('user-agent', '').lower()
                and str(service.get('isInsuran')) == '1'
                and str(service.get('isCustomInsurance', '')) != '1'
                and getattr(self, 'online_insurance', False)
                and getattr(self, 'insurance_city', '').startswith('44')
                and (self.enabled('online.medicare.pay', 'PAYMENT_WEIXIN')
                     or self.enabled('online.medicare.pay', 'PAYMENT_WEIXIN_SL'))
                and bool(value('WEIXIN_ONLINE_MEDICARE_CONFIG', 'country.insurance.mode'))
                and not value('WEIXIN_ONLINE_MEDICARE_CONFIG', 'insurance.tester.information')
                and not value('PAYMENT_PLUGIN', 'online.medicare.pay')
                and not any(self.enabled('online.medicare.pay', t) for t in
                            ['PAYMENT_DAREWAY', 'PAYMENT_NEUSOFT', 'PAYMENT_LIAONING_INSURANCE'])
                and not self.enabled('wechat.insurance.setting', 'HOSPITAL_SYSTEM_CONFIG')
                and self.enabled('country.insurance.no.verify'))

    def check_existing(self):
        target = self.api.params['deptId']
        for path in ['/gateway/registration/current/find', '/gateway/registration/unPaidOrder/judge']:
            rows = self.api.request('GET', path, {'bindingId': self.card['bindingId']})
            if not isinstance(rows, list):
                raise core.LocalError('已有预约查询结构异常，未提交。')
            if any(same_dept(row, target) for row in rows):
                raise core.LocalError('已存在牙体牙髓科预约或待处理订单，守候停止，避免重复。')

    def choose(self, data):
        options = []
        target_name = unquote(self.api.params.get('deptName', ''))
        for doc in data['regInfos']:
            for row in doc.get('scheduleInfos') or []:
                if getattr(self, 'cooldowns', {}).get(self.schedule_key(doc, row), 0) > time.monotonic():
                    continue
                # The query department is a display group (270_03). Each schedule
                # supplies the clinical department; doctor-level fields are empty.
                if (not row.get('deptId') or not target_name
                        or row.get('deptName') != target_name):
                    continue
                if available(row) and money(row.get('regFee')) + money(row.get('treatFee')) <= self.cap:
                    options.append((dict(doc, deptId=row['deptId'], deptName=row['deptName']), row))
        options.sort(key=lambda pair: (pair[1]['regDate'], pair[1].get('timeFlag', ''),
                                       money(pair[1].get('regFee')) + money(pair[1].get('treatFee'))))
        return options[0] if options else None

    def prepare(self, doc, row):
        q = quote
        args = {'branchCode': self.branch['branchCode'], 'deptId': doc['deptId'],
                'deptName': q(row['deptName'], safe="~!*'()"), 'doctorId': doc['doctorId'],
                'regDate': row['regDate'], 'scheduleId': q(row['scheduleId'], safe="~!*'()"),
                'timeFlag': row['timeFlag'], 'ajaxConfig': 'true'}
        times = self.api.request('GET', '/gateway/registration/doctor/time/find', args)
        if not isinstance(times, list):
            raise core.LocalError('分时结构不符，未提交。')
        times = [t for t in times if money(t.get('regLeaveCount', '0')) > 0]
        if not times:
            return None
        slot = sorted(times, key=lambda t: t.get('startTime', ''))[0]
        if not all(slot.get(k) for k in ['periodId', 'startTime', 'endTime']):
            raise core.LocalError('分时缺少号源编号或起止时间，未提交。')
        flow = None
        if self.enabled('registration.patient.flow.verify'):
            flow = self.api.request('POST', '/gateway/registration/support/beforeRegister/window', body={
                'birthday': self.card.get('birthday', ''), 'deptId': doc['deptId'], 'doctorId': doc['doctorId'],
                'healthCardNo': self.patient['healthCardNo'], 'idCardNo': self.patient['patIdno'],
                'patientId': self.card['patId'], 'patientName': self.patient['patName'],
                'phone': self.patient['phoneNo'], 'regDate': row['regDate'], 'shiftName': row['shiftName']})
        extension = None
        if flow:
            digest = hashlib.sha256(flow.get('content', '').encode('utf-8')).hexdigest()
            if (flow.get('title') != '剩余生物样本捐赠知情同意书' or digest != self.settings.get('consent_hash')
                    or str(flow.get('type')) != '2' or str(flow.get('needSignature', '')) == '1'):
                raise core.LocalError('挂号前告知内容发生变化或需要签名，需你在微信处理。')
            agreed = self.settings['donation'] == 'agree'
            suffix = '2' if agreed else '1'
            expected = '同意' if agreed else '不同意'
            if flow.get('btnName' + suffix) != expected:
                raise core.LocalError('告知选项发生变化，未代选。')
            extension = json.dumps({'eventId': flow.get('eventId', ''),
                                    'btnValue': flow['btnValue' + suffix]}, ensure_ascii=False, separators=(',', ':'))
        svc_args = {'bindingId': self.card['bindingId'], 'branchCode': self.branch['branchCode'], 'deptId': doc['deptId']}
        if self.enabled('getSvObject.addScheduleId'):
            svc_args['scheduleId'] = q(slot.get('scheduleId') or row['scheduleId'], safe="~!*'()")
        if self.enabled('getSvObject.addRegDate'):
            svc_args['regDate'] = row['regDate']
        services = self.api.request('GET', '/gateway/registration/svobject/find', svc_args)
        services = [s for s in services if s.get('svObject') == self.settings['service']]
        if len(services) != 1 or str(services[0].get('hasDisease')) == '1':
            raise core.LocalError('服务对象需人工选择病种或不明确，未提交。')
        service = services[0]
        limits = self.api.request('GET', '/gateway/registration/register/limit/info',
            {'tenantId': self.api.identity['tenantId'], 'deptIdOrName': doc['deptId']})
        if not isinstance(limits, list) or any(not isinstance(r, dict) for r in limits):
            raise core.LocalError('实际出诊科室的医保限制结构异常，未提交。')
        if (self.settings['service'] == '医保' and limits
                and str(limits[0].get('appointmentInsuranceLimit')) == '1'):
            raise core.LocalError('该实际出诊科室限制医保预约，未提交。')
        cost_args = dict(args, bindingId=self.card['bindingId'], doctorName=q(doc['doctorName'], safe="~!*'()"),
                         patientId=self.card['patId'], registerTypeId=row.get('registerTypeId', ''),
                         scheduleIdFromSchedule=q(row['scheduleId'], safe="~!*'()"),
                         scheduleIdFromTimeInfo=q(slot.get('scheduleId', ''), safe="~!*'()"),
                         svObjectId=service['svObjectId'], isInsuran=service.get('isInsuran', ''), diseaseId='', applyId='')
        cost_args.pop('scheduleId', None)
        cost_args.pop('ajaxConfig', None)
        country_mode = self.country_insurance_cost_not_needed(service)
        if country_mode:
            cost = {'yhFee': '0'}
            if getattr(self, 'report', None):
                self.report('按网站国家医保预约流程沿用排班费用；保留医保服务对象，不执行支付。')
        else:
            cost = self.api.request('GET', '/gateway/registration/cost/find', cost_args)
        if not isinstance(cost, dict):
            raise core.LocalError('费用结构不符，未提交。')
        reg_fee, treat_fee = cost.get('regFee', row['regFee']), cost.get('treatFee', row['treatFee'])
        if money(reg_fee) + money(treat_fee) > self.cap:
            raise core.LocalError('核定费用超过上限，未提交。')
        payload = {
            'applyId': '', 'affiliatedHospital': doc.get('affiliatedHospital'), 'bindingId': self.card['bindingId'],
            'branchCode': self.branch['branchCode'], 'branchName': self.branch['branchName'],
            'countDeptId': row.get('countDeptId'), 'clinicUnitId': row.get('clinicUnitId'),
            'deptId': doc['deptId'], 'deptName': q(row['deptName'], safe="~!*'()"),
            'diseaseId': None, 'diseaseName': None, 'doctorId': doc['doctorId'], 'doctorName': doc['doctorName'],
            'doctorTitle': doc.get('doctorTitle'), 'doctorLevelCode': doc.get('doctorLevelCode'),
            'scheduleId': slot.get('scheduleId') or row['scheduleId'], 'queueSn': slot.get('periodId', ''),
            'periodId': slot.get('periodId', ''), 'serviceItemId': '', 'timeFlag': row['timeFlag'],
            'svObjectId': service['svObjectId'], 'svObjectName': service.get('svObject', ''), 'svMode': service.get('svMode', ''),
            'cashFee': service.get('cashFee', '0'), 'treatFee': treat_fee, 'regFee': reg_fee,
            'yhFee': cost.get('yhFee', '0.0'), 'insuranFee': cost.get('insuranFee', ''),
            'medicareSettleLogId': cost.get('medicareSettleLogId', ''), 'patientId': self.card['patId'],
            'idCardNo': self.patient['patIdno'], 'phone': self.patient['phoneNo'], 'orderType': '', 'registerTypeId': '',
            'startTime': slot.get('startTime', ''), 'endTime': slot.get('endTime', ''),
            'regDate': row['regDate'], 'shiftName': row['shiftName'], 'remark': '', 'roomAddress': '',
            'connect_redirect': 1, 'guidanceCallback': '', 'ajaxConfig': True,
            'isTencentHealth': '', 'tencentHealthId': '', 'alipayHealthId': '',
            'nonceStr': str(secrets.randbelow(10**17)).zfill(17), 'timestamp': int(time.time() * 1000)}
        self.order_path = INSURANCE_ORDER_PATH if country_mode else ORDER_PATH
        if country_mode:
            # The published client uses this ordinary insurance reservation
            # endpoint and these fixed fields with the no-verification setting.
            payload.update(insuranFee='0', medicareSettleLogId='ElectronicMedicalInsurance',
                           scheduleIdFromSchedule=q(row['scheduleId'], safe="~!*'()"))
        if extension:
            payload['beforeDiagnosisExtend'] = extension
        if service.get('cityId'):
            payload['cityId'] = service['cityId']
        payload['signature'] = site_crypto.sign({k: payload[k] for k in ['deptId', 'doctorId', 'scheduleId', 'bindingId', 'nonceStr', 'timestamp']})
        duplicate = self.api.request('POST', '/gateway/registration/history/judge', body={
            k: payload[k] for k in ['bindingId', 'svObjectId', 'deptId', 'doctorId', 'scheduleId',
                                  'queueSn', 'timeFlag', 'regDate', 'startTime', 'endTime', 'registerTypeId']})
        if not isinstance(duplicate, dict) or str(duplicate.get('flag')) == '1':
            raise core.LocalError('医院提示存在重复预约或需确认，未提交。')
        self.check_existing()
        return payload

    def attempt(self, data):
        with Mutex():
            if (core.DATA / 'booking-journal.dpapi').exists():
                raise core.LocalError('已存在一次预约提交记录，需人工核对结果后才可再次提交。')
            selected = self.choose(data)
            if selected is None:
                return None
            doc, row = selected
            payload = self.prepare(doc, row)
            if payload is None:
                self.defer(doc, row, 5)
                return None
            if getattr(self, 'cancel_event', None) is not None and self.cancel_event.is_set():
                raise core.LocalError('已停止守候，未提交预约。')
            record = {'state': 'sending', 'doctor': doc['doctorName'], 'date': row['regDate'],
                      'shift': row['shiftName'], 'start': payload['startTime'], 'end': payload['endTime'],
                      'time': dt.datetime.now(TZ).isoformat()}
            record['path'] = getattr(self, 'order_path', ORDER_PATH)
            write_private('booking-journal.dpapi', record)
            try:
                result = self.submit(payload)
            except Exception:
                record['state'] = 'unknown'
                write_private('booking-journal.dpapi', record)
                raise core.LocalError('提交结果不明确，已停止且不会重发。请在微信挂号记录核对。') from None
            if not isinstance(result, dict) or str(result.get('resultCode')) != '0':
                record['state'] = 'rejected'
                record['code'] = str(result.get('resultCode', ''))[:20] if isinstance(result, dict) else ''
                write_private('booking-journal.dpapi', record)
                raise core.LocalError('医院未确认提交成功，已停止。请在微信查看提示或挂号记录。')
            response = result.get('data')
            record['response'] = response
            record['state'] = 'submitted'
            write_private('booking-journal.dpapi', record)
            # Mode 1 is the public client's lock-success route; mode 0 leads to payment.
            if isinstance(response, dict) and response.get('registerMode') == 1:
                label = '医院已确认预约锁号成功；未付款。请到微信挂号记录核对就诊信息。'
            elif isinstance(response, dict) and response.get('registerMode') == 0:
                label = '订单已生成，但医院要求支付。脚本未付款；请在微信核对待支付订单。'
            else:
                label = '医院已接收提交，需在微信核对最终状态。脚本未付款。'
            return f"{record['doctor']}  {record['date']}  {record['start']}–{record['end']}\n{label}"

    def submit(self, payload):
        headers = dict(self.api.session['headers'])
        headers.update({'Content-Type': 'application/json;charset=UTF-8', 'Accept': 'application/json',
                        'Origin': 'https://' + core.HOST,
                        'Referer': 'https://' + core.HOST + '/patient/registration/'})
        path = getattr(self, 'order_path', ORDER_PATH)
        if path not in {ORDER_PATH, INSURANCE_ORDER_PATH}:
            raise core.LocalError('预约接口不在已核对范围内，未提交。')
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                   if path == INSURANCE_ORDER_PATH
                   else json.dumps(site_crypto.encrypt_payload(payload)).encode('utf-8'))
        conn = http.client.HTTPSConnection(core.HOST, timeout=60, context=ssl.create_default_context())
        try:
            conn.request('POST', path, encoded, headers)
            response = conn.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 4 * 1024 * 1024:
                raise core.LocalError('提交结果未确认。')
            return json.loads(raw)
        finally:
            conn.close()
