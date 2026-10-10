"""Read-only CLI independent of desktop dependencies."""
from .core import Api, load_credentials, target_dates, choices


def probe():
    api = Api(load_credentials())
    profile = api.profile()
    court = api.tennis()
    type_id = court["ground_type_id"]
    print("login_ok=True; direct_booking_allowed=" + str(profile.get("require_verification") == 1))
    print("dates=" + ",".join(str(x["date"]) for x in api.dates(type_id)))
    print("notices_received=" + str(bool(api.notices(type_id))))
    for target in target_dates(api.dates(type_id)):
        slots = choices(api.grid(type_id, target[0]), target)
        print("target=" + " ".join(target) + "; matched=" + str(len(slots)) +
              "; available=" + str(sum(s.get("status") == 1 for s in slots)))
    print("order_query_ok=True; count=" + str(len(api.orders())))
