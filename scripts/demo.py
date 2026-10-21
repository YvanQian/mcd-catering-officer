"""麦麦团餐官 —— 一键演示。

跑一遍完整决策链路：
    选地址 → 找团餐门店 → 挑档期 → 按预算配餐 → 三服务比价 → dry-run 下单

用法：
    python scripts/demo.py
    python scripts/demo.py --headcount 30 --budget 35
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

from catering import CateringOfficer, render_compare, render_plan
from mcd_client import McdError


def title(text: str) -> None:
    print()
    print("=" * 64)
    print(text)
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headcount", type=int, default=20)
    parser.add_argument("--budget", type=float, default=40.0, help="人均预算（元）")
    parser.add_argument("--address-id", default=None)
    args = parser.parse_args()

    officer = CateringOfficer()

    title("1 / 5　确认配送目的地")
    address = officer.pick_address(args.address_id)
    print(f"收货人　{address.get('contactName', '')}")
    print(f"地址　　{address.get('fullAddress', '')}")
    print(f"电话　　{address.get('phone', '')}")

    title("2 / 5　查找可配送的团餐门店")
    stores = officer.stores(address["addressId"])
    if not stores:
        print("该地址附近没有团餐门店。")
        return 1
    for s in stores:
        print(f"· {s.name}　{s.store_code}　营业 {s.open_time}-{s.close_time}　"
              f"{len(s.slots)} 个可约档期")
    store = stores[0]

    title("3 / 5　锁定档期")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    candidates = [s for s in store.slots if s.date == tomorrow and s.name == "午餐"]
    if not candidates:
        candidates = [s for s in store.slots if s.date == tomorrow]
    slot = candidates[0]
    reservation = f"{slot.date} {slot.start}"
    ok, msg = CateringOfficer.lead_time_check(reservation)
    print(f"选定档期　{slot.date} {slot.name} {slot.start}-{slot.end}")
    print(f"提前量　　{msg}")

    title("4 / 5　按预算配餐")
    plan = officer.plan(store, args.headcount, args.budget, reservation, "GMS001")
    print(render_plan(plan))

    title("5 / 5　同样是这单，换助餐服务要多少钱")
    quotes = officer.compare_services(store, plan["items"], reservation)
    print(render_compare(quotes, args.headcount))
    if len(quotes) >= 2:
        gap = quotes[-1].payable - quotes[0].payable
        print()
        print(f"→ 「{quotes[-1].service_name}」比「{quotes[0].service_name}」多花 ¥{gap:.2f}")
        print(f"→ 若确实需要摆台，这就是它的真实价格；若只是要送到工位，直接省下 ¥{gap:.2f}")

    title("下单参数（dry-run，未真实提交）")
    payload = officer.build_order(
        store, plan["items"], quotes[0].service_code, address["addressId"], reservation
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except McdError as exc:
        raise SystemExit(f"[错误] {exc}")
