"""Venue API client. No payment/cancellation API, no automatic POST retries."""
import datetime as dt
import http.client
import json
import math
import re
import uuid
from common.errors import LocalError as BookingError
from common.storage import project_data_dir, read_json, write_json

HOST = "spc.dachanbay.tencent.com"
VENUE = "898bd950-d659-4ca1-bac4-5eebdd1b07cd"
TZ = dt.timezone(dt.timedelta(hours=8))
TARGETS = {1: ("21:00", "22:00"), 2: ("19:00", "20:00")}
DATA = project_data_dir('dachanbay_tennis')


def save_credentials(value):
    token = value.get("token", "").strip()
    if not token or "\r" in token or "\n" in token:
        raise BookingError("凭证为空或格式不正确。")
    value = {k: str(value.get(k, "")) for k in ("token", "user_agent", "cookie", "mp_openid")}
    value["token"] = token
    write_json(DATA / 'credentials.dpapi', value)


def load_credentials():
    path = DATA / "credentials.dpapi"
    if not path.exists():
        raise BookingError("尚未获取登录凭证。请先完成步骤 1。")
    try:
        return read_json(path)
    except BookingError:
        raise
    except Exception:
        raise BookingError("本地凭证文件无法读取，请重新获取。") from None


class Api:
    def __init__(self, credentials):
        self.credentials = credentials
        self.biz = str(uuid.uuid4())

    def call(self, endpoint, body=None):
        if not re.fullmatch(r"h5-(basic|venue)-api/[A-Za-z]+", endpoint):
            raise BookingError("不支持的接口路径。")
        headers = {"Content-Type": "application/json", "spc-jwt-token": self.credentials["token"],
                   "x-rasse-biz-id": self.biz, "Origin": f"http://{HOST}",
                   "Referer": f"http://{HOST}/index"}
        for key, field in (("User-Agent", "user_agent"), ("Cookie", "cookie")):
            if self.credentials.get(field):
                headers[key] = self.credentials[field]
        conn = http.client.HTTPConnection(HOST, 80, timeout=20)
        try:
            conn.request("POST", "/api/" + endpoint, json.dumps(body or {}).encode(), headers)
            response = conn.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status == 401:
                raise BookingError("登录已过期，请在微信刷新登录后重新获取凭证。")
            if response.status != 200:
                raise BookingError(f"接口 {endpoint.split('/')[-1]} 返回 HTTP {response.status}；未重试。")
            code = response.getheader("trpc-func-ret") or response.getheader("trpc-ret") or "0"
            if code != "0" or response.getheader("trpc-error-msg"):
                # Do not echo server messages: they may include credentials or user data.
                raise BookingError(f"接口 {endpoint.split('/')[-1]} 业务检查未通过（代码 {code[:12]}）。请在微信查看提示。")
            if len(raw) > 4 * 1024 * 1024:
                raise BookingError("接口响应过大，已停止。")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise BookingError("接口响应格式与网站前端不符，已停止。")
            return result
        except BookingError:
            raise
        except Exception:
            raise BookingError(f"接口 {endpoint.split('/')[-1]} 请求失败或结果不明确；未重试。") from None
        finally:
            conn.close()

    def venue(self, name, body=None):
        return self.call("h5-venue-api/" + name, body)

    def profile(self):
        result = self.call("h5-basic-api/GetInfoByUserId")
        if "require_verification" not in result:
            raise BookingError("用户信息结构需要核对，已停止。")
        return result

    def tennis(self):
        result = self.venue("GetGroundTypeList", {"venue_id": VENUE})
        candidates = [x for x in result.get("data", []) if "网球" in x.get("ground_type_name", "")]
        if len(candidates) != 1 or not candidates[0].get("ground_type_id"):
            raise BookingError("未找到唯一网球项目，需要核对场馆返回数据。")
        return candidates[0]

    def notices(self, type_id):
        result = []
        for name in ("GetGroundNotice", "GetGroundBuyNotice"):
            data = self.venue(name, {"ground_type_id": type_id})
            if not isinstance(data.get("notice"), str):
                raise BookingError("须知格式不符，已停止。")
            result.append(data["notice"])
        return "\n".join(result).replace(";", "\n")

    def dates(self, type_id):
        result = self.venue("GetAppointmentDateList", {"ground_type_id": type_id})
        return require_list(result, "items")

    def grid(self, type_id, date):
        result = self.venue("GetVenueAppointmentList", {"ground_type_id": type_id, "date": date})
        if int(result.get("mode", 0)) != 1:
            raise BookingError("网球项目不是时段预约模式，已停止。")
        require_list(result, "items")
        return result

    def orders(self):
        return require_list(self.venue("GetOrderSummaryList", {"venue_id": VENUE}), "data")


def require_list(data, key):
    if not isinstance(data.get(key), list):
        raise BookingError(f"接口缺少 {key} 列表，已停止。")
    return data[key]


def hhmm(value):
    text = str(value).replace("T", " ").split(" ")[-1]
    return text[:5] if re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", text) else ""


def target_dates(items, now=None):
    now = now or dt.datetime.now(TZ)
    targets = []
    for item in items:
        date = str(item.get("date", ""))
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            raise BookingError("预约日期格式不符，已停止。") from None
        if day.weekday() in TARGETS:
            start, end = TARGETS[day.weekday()]
            if dt.datetime.fromisoformat(date + " " + start).replace(tzinfo=TZ) > now:
                targets.append((date, start, end))
    return sorted(set(targets))


def choices(grid, target):
    date, start, end = target
    result = []
    for court in grid["items"]:
        if not court.get("ground_id"):
            raise BookingError("返回场地缺少编号，已停止。")
        for slot in require_list(court, "booking_durations"):
            if (hhmm(slot.get("start_time")), hhmm(slot.get("end_time"))) != (start, end):
                continue
            price = slot.get("price")
            if (isinstance(price, bool) or not isinstance(price, (int, float))
                    or not math.isfinite(price) or price < 0 or price != int(price)):
                raise BookingError("价格格式不符，已停止。")
            result.append({**slot, "ground_id": court["ground_id"],
                           "ground_name": court.get("ground_name", "网球场"), "date": date})
    return result


def duplicate(orders, target):
    date, start, end = target
    for order in orders:
        # Be conservative: even a cancelled matching order needs manual review.
        order_date = str(order.get("date", ""))[:10]
        if not order_date:
            order_date = str(order.get("start_time", ""))[:10]
        if order_date != date:
            continue
        a, b = hhmm(order.get("start_time")), hhmm(order.get("end_time"))
        if not a or not b:
            raise BookingError("当日订单时段结构不明确，请先在微信核对订单。")
        if a < end and b > start:
            return True
    return False


class Journal:
    def __init__(self, path=None):
        self.path = path or DATA / "attempts.dpapi"
        if self.path.exists():
            try:
                self.records = read_json(self.path)
                if not isinstance(self.records, dict):
                    raise ValueError()
            except Exception:
                raise BookingError("下单记录损坏，停止提交。请人工核对微信订单。") from None
        else:
            self.records = {}

    @staticmethod
    def key(target):
        return "|".join(target)

    def contains(self, target):
        return self.key(target) in self.records

    def begin(self, target, slot):
        if self.contains(target):
            raise BookingError("该目标已有下单尝试记录，不会再次提交。请核对微信订单。")
        self.records[self.key(target)] = {"state": "unknown", "ground_id": slot["ground_id"],
                                          "ground_name": slot.get("ground_name", "网球场"),
                                          "price_cents": slot.get("price"),
                                          "time": dt.datetime.now(TZ).isoformat()}
        write_json(self.path, self.records)

    def finish(self, target, response):
        self.records[self.key(target)].update(state="response_received",
            main_order_no=response["main_order_no"], need_payment=response.get("need_payment"))
        write_json(self.path, self.records)


def validate_slot(grid, slot, cap_cents):
    if slot.get("status") != 1:
        raise BookingError("该时段需要额外确认或不可预约，请在微信操作。")
    if slot.get("consumption_rule") == 2:
        raise BookingError("该时段要求腾讯验证码，请在微信完成预约；脚本不会绕过。")
    if "consumption_rule" not in slot or "duration_fee_type" not in slot:
        raise BookingError("预约条件字段不完整，已停止。")
    if cap_cents is not None and slot["price"] > cap_cents:
        raise BookingError("该时段价格超过你设置的单次上限。")
    fee = slot["duration_fee_type"]
    if fee == 3:
        low, high = grid.get("pay_min_booking_count"), grid.get("pay_max_booking_count")
    elif fee in (1, 2):
        low, high = grid.get("free_min_booking_count"), grid.get("free_max_booking_count")
        limit_key = "enterprise_free_max_booking_count" if fee == 1 else "free_max_booking_count"
        used_key = "enterprise_free_exist_count" if fee == 1 else "society_free_exist_count"
        limit, used = grid.get(limit_key), grid.get(used_key)
        if not isinstance(limit, (int, float)) or not isinstance(used, (int, float)) or used >= limit:
            raise BookingError("公益/企业时段次数不满足，已停止。")
    else:
        raise BookingError("未知收费类型，已停止。")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)) or not low <= 1 <= high:
        raise BookingError("单小时预约不符合服务器返回的数量限制。")


def submit(api, journal, type_id, target, slot, cap_cents, stop):
    if stop.is_set():
        raise BookingError("已停止。")
    if api.profile().get("require_verification") != 1:
        raise BookingError("账户需要资格/手机号确认，请先在微信完成。")
    if duplicate(api.orders(), target):
        raise BookingError("目标时段已有订单，停止重复提交。")
    fresh = api.grid(type_id, target[0])
    matches = [x for x in choices(fresh, target) if x["ground_id"] == slot["ground_id"]]
    if len(matches) != 1:
        raise BookingError("余量发生变化，已停止。")
    validate_slot(fresh, matches[0], cap_cents)
    date, start, end = target
    slots = [{"ground_id": slot["ground_id"], "start_time": f"{date} {start}:00",
              "end_time": f"{date} {end}:00"}]
    check = api.venue("CheckIsViolation", {"venue_id": VENUE, "check_type": 1, "slots": slots})
    if check.get("is_violation") is not False:
        raise BookingError("资格检查未明确通过，请在微信核对。")
    if stop.is_set():
        raise BookingError("已停止。")
    journal.begin(target, matches[0])
    response = api.venue("PayBooking", {"scene": 2,
        "mp_openid": api.credentials.get("mp_openid", ""), "slots": slots,
        "confirm_booking_agreement": 2})
    if not response.get("main_order_no") or not isinstance(response.get("need_payment"), bool):
        raise BookingError("已发出下单请求，结果不明确。请立即在微信核对订单，禁止重复提交。")
    journal.finish(target, response)
    return response
