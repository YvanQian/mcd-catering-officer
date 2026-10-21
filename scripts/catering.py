"""麦麦团餐官 —— 企业团餐决策引擎。

核心能力：
1. 门店发现与档期解析（团餐走预约制，必须先确定时段）
2. 助餐服务横向比价（同一单跑遍所有服务，算出实付差价）
3. 折扣解锁提醒（还差多少元能进下一档）
4. 按人数与人均预算自动配餐，并用真实算价接口校验
5. 提前量校验（专人分餐/摆台需提前 4 小时预约）
6. 下单默认 dry-run，只输出参数不真实提交

仅使用 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from mcd_client import McdError, McdMCP

CENT = 100
RESERVATION_FORMAT = "%Y-%m-%d %H:%M"
LEAD_TIME = timedelta(hours=4)

# 配餐时各餐位占人均预算的比例，按「主食 > 小食 > 饮品」分配
SLOT_RATIOS = (("主食", 0.46), ("小食甜品", 0.30), ("饮品", 0.22))
SKIP_CATEGORIES = {"人气热卖", "开心乐园"}


# ---------------------------------------------------------------- 数据结构


@dataclass
class Slot:
    """门店可预约时段。"""

    date: str
    name: str
    start: str
    end: str
    is_today: bool = False

    @property
    def label(self) -> str:
        return f"{self.date} {self.name} {self.start}-{self.end}"

    def start_dt(self) -> datetime:
        return datetime.strptime(f"{self.date} {self.start}", RESERVATION_FORMAT)


@dataclass
class Store:
    store_code: str
    be_code: str
    name: str
    address: str = ""
    reservation: bool = False
    business_status: bool = False
    open_time: str = ""
    close_time: str = ""
    slots: list[Slot] = field(default_factory=list)


@dataclass
class MenuItem:
    code: str
    name: str
    price: float
    original_price: float
    category: str
    tags: list[str] = field(default_factory=list)
    can_with_order: bool = False


@dataclass
class Service:
    code: str
    name: str
    items: list[str]
    enable: bool
    reason: str = ""


@dataclass
class Quote:
    service_code: str
    service_name: str
    original: float
    payable: float
    discount: float
    promo: str = ""
    next_discount: float = 0.0
    next_gap: float = 0.0
    delivery: float = 0.0
    packing: float = 0.0

    @property
    def rate(self) -> float:
        return self.payable / self.original if self.original else 1.0

    @property
    def rate_label(self) -> str:
        return f"{self.rate * 10:.1f}折"


# ---------------------------------------------------------------- 工具函数


def display_width(text: str) -> int:
    """估算终端显示宽度，中日韩字符按 2 列计算。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))


def pad(text: str, width: int, align: str = "left") -> str:
    gap = max(width - display_width(text), 0)
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def table(headers: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> str:
    aligns = aligns or ["left"] * len(headers)
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))
    line = "  ".join(pad(h, widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * widths[i] for i in range(len(headers)))]
    for row in rows:
        out.append("  ".join(pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)))
    return "\n".join(out)


def parse_slots(store_code: str, options: Iterable[dict]) -> list[Slot]:
    """把 "早餐(08:00至11:59)，午餐(12:00至15:59)" 解析成结构化档期。"""
    slots: list[Slot] = []
    for opt in options or []:
        date = opt.get("date", "")
        is_today = bool(opt.get("today"))
        text = opt.get("reservationOptionText", "") or ""
        for name, body in re.findall(r"([^，,()（）]+)[(（]([^)）]+)[)）]", text):
            for start, end in re.findall(r"(\d{1,2}:\d{2})\s*至\s*(\d{1,2}:\d{2})", body):
                slots.append(Slot(date=date, name=name.strip(), start=start, end=end, is_today=is_today))
    return slots


# ---------------------------------------------------------------- 业务引擎


class CateringOfficer:
    def __init__(self, client: McdMCP | None = None):
        self.client = client or McdMCP()

    # -- 地址 ----------------------------------------------------------
    def addresses(self) -> list[dict]:
        data, _ = self.client.call("delivery-query-addresses")
        return (data or {}).get("data", {}).get("addresses", []) or []

    def pick_address(self, address_id: str | None = None) -> dict:
        items = self.addresses()
        if not items:
            raise McdError("该账号下没有麦乐送收货地址，团餐需要地址作为配送目的地。")
        if address_id:
            for a in items:
                if a.get("addressId") == address_id:
                    return a
            raise McdError(f"地址 {address_id} 不存在，可用：{[a['addressId'] for a in items]}")
        return items[0]

    # -- 门店 ----------------------------------------------------------
    def stores(self, address_id: str) -> list[Store]:
        data, _ = self.client.call("delivery-query-stores", beType=6, addressId=address_id)
        result = []
        for raw in (data or {}).get("data", []) or []:
            result.append(
                Store(
                    store_code=raw.get("storeCode", ""),
                    be_code=raw.get("beCode", ""),
                    name=raw.get("storeName", ""),
                    address=raw.get("address", ""),
                    reservation=bool(raw.get("reservation")),
                    business_status=bool(raw.get("businessStatus")),
                    open_time=raw.get("businessStartTime", ""),
                    close_time=raw.get("businessEndTime", ""),
                    slots=parse_slots(raw.get("storeCode", ""), raw.get("reservationTimeOptions")),
                )
            )
        return result

    # -- 菜单 ----------------------------------------------------------
    def menu(self, store: Store, reservation: str | None = None) -> list[MenuItem]:
        kwargs = dict(storeCode=store.store_code, orderType=2, beType=6, beCode=store.be_code)
        if reservation:
            kwargs["reservationDate"] = reservation
        data, _ = self.client.call("query-meals", **kwargs)
        payload = (data or {}).get("data", {}) or {}
        catalog = payload.get("meals", {}) or {}
        items: list[MenuItem] = []
        seen: set[str] = set()
        for cat in payload.get("categories", []) or []:
            cat_name = cat.get("name", "")
            for entry in cat.get("meals", []) or []:
                code = entry.get("code", "")
                meta = catalog.get(code) or {}
                if not code or code in seen or not meta.get("name"):
                    continue
                seen.add(code)
                items.append(
                    MenuItem(
                        code=code,
                        name=meta.get("name", ""),
                        price=float(meta.get("currentPrice") or 0),
                        original_price=float(meta.get("originalPrice") or 0),
                        category=cat_name,
                        tags=list(entry.get("tags") or []),
                        can_with_order=bool(meta.get("canWithOrder")),
                    )
                )
        return items

    # -- 助餐服务 ------------------------------------------------------
    def services(self, store: Store, reservation: str | None = None) -> list[Service]:
        kwargs = dict(storeCode=store.store_code, beType=6, orderType=2, beCode=store.be_code)
        if reservation:
            kwargs["reservationDate"] = reservation
        data, _ = self.client.call("query-meal-assistance", **kwargs)
        out = []
        for raw in (data or {}).get("data", {}).get("mealAssistanceItems", []) or []:
            out.append(
                Service(
                    code=raw.get("gmServiceCode", ""),
                    name=raw.get("gmServiceName", ""),
                    items=list(raw.get("serviceItems") or []),
                    enable=bool(raw.get("enable")),
                    reason=raw.get("unusableReason", "") or "",
                )
            )
        return out

    # -- 算价 ----------------------------------------------------------
    def quote(self, store: Store, items: list[dict], service_code: str, reservation: str | None = None) -> Quote:
        kwargs = dict(
            storeCode=store.store_code,
            orderType=2,
            beType=6,
            beCode=store.be_code,
            gmServiceCode=service_code,
            items=items,
            needTableware=True,
        )
        if reservation:
            kwargs["reservationDate"] = reservation
        data, _ = self.client.call("calculate-price", **kwargs)
        body = (data or {}).get("data") or {}
        if not body:
            raise McdError(f"算价失败：{(data or {}).get('message', '未知原因')}")
        enjoyed = body.get("enjoyed") or {}
        enjoyable = body.get("enjoyable") or {}
        names = [m.get("name", "") for m in body.get("mealAssistanceList", []) or []]
        return Quote(
            service_code=service_code,
            service_name=names[0] if names else service_code,
            original=body.get("originalPrice", 0) / CENT,
            payable=body.get("price", 0) / CENT,
            discount=body.get("discount", 0) / CENT,
            promo=enjoyed.get("promotionName") or "",
            next_discount=float(enjoyable.get("realDiscount") or 0),
            next_gap=(enjoyable.get("balance") or 0) / CENT,
            delivery=body.get("deliveryPrice", 0) / CENT,
            packing=body.get("packingPrice", 0) / CENT,
        )

    def compare_services(
        self, store: Store, items: list[dict], reservation: str | None = None
    ) -> list[Quote]:
        """同一单跑遍所有可用助餐服务，按实付从低到高排序。"""
        quotes = []
        for svc in self.services(store, reservation):
            if not svc.enable:
                continue
            try:
                quotes.append(self.quote(store, items, svc.code, reservation))
            except McdError:
                continue
        return sorted(quotes, key=lambda q: q.payable)

    # -- 配餐 ----------------------------------------------------------
    def _pick(self, pool: dict[str, list[MenuItem]], target: float, headcount: int, scale: float) -> dict[str, MenuItem]:
        chosen: dict[str, MenuItem] = {}
        for slot_name, ratio in SLOT_RATIOS:
            bucket = pool.get(slot_name)
            if not bucket:
                continue
            cap = target * ratio / headcount * scale
            affordable = [i for i in bucket if i.price <= cap]
            chosen[slot_name] = affordable[0] if affordable else bucket[-1]
        return chosen

    def _snapshot(
        self,
        store: Store,
        pool: dict[str, list[MenuItem]],
        target: float,
        headcount: int,
        scale: float,
        service_code: str,
        reservation: str | None,
    ) -> dict:
        chosen = self._pick(pool, target, headcount, scale)
        items = [{"productCode": i.code, "quantity": headcount} for i in chosen.values()]
        quote = self.quote(store, items, service_code, reservation)
        return {"chosen": chosen, "items": items, "quote": quote, "scale": scale}

    def plan(
        self,
        store: Store,
        headcount: int,
        budget_per_head: float,
        reservation: str | None = None,
        service_code: str = "GMS001",
    ) -> dict:
        """按人数与人均预算配餐，用真实算价接口做二分收敛，尽量把预算吃满。

        思路：把「各餐位价格上限」乘一个全局系数 scale，scale 越大配得越贵。
        scale 与实付近似单调递增，因此先用指数扩张找到可行上界，再二分逼近，
        全程以 calculate-price 的真实折后价为准，而不是菜单标价加总。
        """
        menu = self.menu(store, reservation)
        if not menu:
            raise McdError("该门店当前没有返回团餐菜单。")

        pool: dict[str, list[MenuItem]] = {}
        for item in menu:
            if item.price <= 0 or item.category in SKIP_CATEGORIES:
                continue
            pool.setdefault(item.category, []).append(item)
        for bucket in pool.values():
            bucket.sort(key=lambda x: x.price, reverse=True)
        if not pool:
            raise McdError("菜单里没有可用于配餐的分类。")

        target = headcount * budget_per_head
        calls = 0

        def evaluate(scale: float) -> dict:
            nonlocal calls
            calls += 1
            return self._snapshot(store, pool, target, headcount, scale, service_code, reservation)

        best = evaluate(0.25)
        if best["quote"].payable > target:
            raise McdError(
                f"在人均 ¥{budget_per_head:.2f} 的预算下无法配出合规菜单，"
                f"最低方案已需 ¥{best['quote'].payable / headcount:.2f}/人，建议提高预算或减少人数。"
            )

        lo, hi = 0.25, 4.0
        probe = 0.5
        while probe <= 4.0:
            snap = evaluate(probe)
            if snap["quote"].payable > target:
                hi = probe
                break
            best, lo = snap, probe
            if best["quote"].payable >= target * 0.97:
                break
            probe *= 1.7

        for _ in range(6):
            if best["quote"].payable >= target * 0.97:
                break
            mid = (lo + hi) / 2
            snap = evaluate(mid)
            if snap["quote"].payable <= target:
                best, lo = snap, mid
            else:
                hi = mid

        quote = best["quote"]
        return {
            "store": store,
            "headcount": headcount,
            "budget_per_head": budget_per_head,
            "target": target,
            "items": best["items"],
            "detail": list(best["chosen"].values()),
            "quote": quote,
            "per_head": quote.payable / headcount if headcount else 0,
            "reservation": reservation,
            "attempts": calls,
        }

    # -- 下单（默认 dry-run）-------------------------------------------
    def build_order(
        self,
        store: Store,
        items: list[dict],
        service_code: str,
        address_id: str,
        reservation: str,
        remark: str = "麦麦团餐官生成",
    ) -> dict:
        return {
            "storeCode": store.store_code,
            "beCode": store.be_code,
            "beType": 6,
            "orderType": 2,
            "addressId": address_id,
            "gmServiceCode": service_code,
            "reservationDate": reservation,
            "needTableware": True,
            "remark": remark,
            "items": items,
        }

    def submit_order(self, payload: dict, dry_run: bool = True) -> dict:
        if dry_run:
            return {"dryRun": True, "payload": payload}
        data, _ = self.client.call("create-order", **payload)
        return data or {}

    # -- 提前量校验 ----------------------------------------------------
    @staticmethod
    def lead_time_check(reservation: str, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now()
        try:
            target = datetime.strptime(reservation, RESERVATION_FORMAT)
        except ValueError:
            return False, "预约时间格式应为 yyyy-MM-dd HH:mm"
        gap = target - now
        if gap < timedelta(0):
            return False, f"预约时间已过期（{reservation}）"
        if gap < LEAD_TIME:
            hours = gap.total_seconds() / 3600
            return False, (
                f"距预约仅 {hours:.1f} 小时，不足 4 小时——专人分餐/专人摆台将不可选，"
                f"只能使用保鲜速达"
            )
        return True, f"距预约 {(gap.total_seconds() / 3600):.1f} 小时，满足提前 4 小时要求"


# ---------------------------------------------------------------- 报告输出


def render_plan(result: dict) -> str:
    store: Store = result["store"]
    q: Quote = result["quote"]
    lines = []
    lines.append(f"门店　{store.name}（{store.store_code}）")
    lines.append(f"时段　{result['reservation'] or '未指定（按当前可售）'}")
    lines.append(f"规模　{result['headcount']} 人，人均预算 ¥{result['budget_per_head']:.2f}，目标 ¥{result['target']:.2f}")
    lines.append("")
    rows = []
    for item in result["detail"]:
        rows.append([
            item.category,
            item.name,
            f"¥{item.price:.2f}",
            str(result["headcount"]),
            f"¥{item.price * result['headcount']:.2f}",
        ])
    lines.append(table(["分类", "餐品", "单价", "份数", "小计"], rows, ["left", "left", "right", "right", "right"]))
    lines.append("")
    lines.append(f"助餐服务　{q.service_name}" + (f"　｜　活动 {q.promo}" if q.promo else ""))
    lines.append(
        f"算价　　原价 ¥{q.original:.2f} → 实付 ¥{q.payable:.2f}，优惠 ¥{q.discount:.2f}（实付 {q.rate_label}）"
    )
    lines.append(f"人均　　实付 ¥{result['per_head']:.2f}，预算余量 ¥{result['target'] - q.payable:.2f}")
    if q.next_discount:
        lines.append(f"加购　　按原价再凑 ¥{q.next_gap:.2f} 可解锁 {q.next_discount:.1f} 折")
    return "\n".join(lines)


def render_compare(quotes: list[Quote], headcount: int) -> str:
    if not quotes:
        return "没有可用的助餐服务。"
    rows = []
    base = quotes[-1].payable
    for q in quotes:
        rows.append([
            q.service_name,
            q.promo or "—",
            q.rate_label,
            f"¥{q.payable:.2f}",
            f"¥{q.payable / headcount:.2f}",
            f"省 ¥{base - q.payable:.2f}",
        ])
    return table(["助餐服务", "促销", "折扣", "实付", "人均", "相对最高价"], rows,
                 ["left", "left", "right", "right", "right", "right"])


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="麦麦团餐官 —— 基于麦当劳 MCP 的企业团餐决策引擎")
    p.add_argument("--address-id", help="配送地址 ID，不传则用账号第一个地址")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("addresses", help="列出账号下的收货地址")

    sp = sub.add_parser("stores", help="查询可配送的团餐门店及其档期")
    sp.add_argument("--slots", action="store_true", help="同时展开未来档期")

    sp = sub.add_parser("compare", help="同一单对比所有助餐服务的实付差异")
    sp.add_argument("--headcount", type=int, default=20)
    sp.add_argument("--budget", type=float, default=40.0, help="人均预算")
    sp.add_argument("--at", help="预约时间 yyyy-MM-dd HH:mm")

    sp = sub.add_parser("plan", help="按人数与预算自动配餐")
    sp.add_argument("--headcount", type=int, default=20)
    sp.add_argument("--budget", type=float, default=40.0, help="人均预算")
    sp.add_argument("--at", help="预约时间 yyyy-MM-dd HH:mm")
    sp.add_argument("--service", default="GMS001", help="助餐服务码，默认 GMS001 保鲜速达")
    sp.add_argument("--submit", action="store_true", help="真实下单（默认 dry-run）")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        officer = CateringOfficer()
    except McdError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "addresses":
            rows = [[a.get("contactName", ""), a.get("fullAddress", ""), a.get("phone", ""), a.get("addressId", "")]
                    for a in officer.addresses()]
            print(officer and table(["联系人", "地址", "电话", "地址ID"], rows))
            return 0

        address = officer.pick_address(args.address_id)
        stores = officer.stores(address["addressId"])
        if not stores:
            print("该地址附近没有可配送的团餐门店。", file=sys.stderr)
            return 1

        if args.cmd == "stores":
            rows = []
            for s in stores:
                rows.append([s.name, s.store_code, s.open_time + "-" + s.close_time,
                             "是" if s.business_status else "否", f"{len(s.slots)} 个"])
            print(table(["门店", "门店编码", "营业时间", "营业中", "可约档期"], rows))
            if args.slots:
                print()
                for s in stores:
                    print(f"· {s.name}")
                    for slot in s.slots:
                        flag = "（今天）" if slot.is_today else ""
                        print(f"    {slot.date} {slot.name} {slot.start}-{slot.end}{flag}")
            return 0

        store = stores[0]
        if args.at:
            ok, msg = CateringOfficer.lead_time_check(args.at)
            print(f"[提前量] {msg}")
            print()

        menu = officer.menu(store, args.at)
        if not menu:
            print("该门店没有返回团餐菜单。", file=sys.stderr)
            return 1

        service = getattr(args, "service", "GMS001")
        plan = officer.plan(store, args.headcount, args.budget, args.at, service)
        items = plan["items"]

        if args.cmd == "compare":
            print(render_plan(plan))
            print()
            print("同一单，换一种助餐服务要多少钱：")
            quotes = officer.compare_services(store, items, args.at)
            print(render_compare(quotes, args.headcount))
            if len(quotes) >= 2:
                diff = quotes[-1].payable - quotes[0].payable
                print()
                print(f"结论：选「{quotes[0].service_name}」比「{quotes[-1].service_name}」少花 ¥{diff:.2f}")
            return 0

        if args.cmd == "plan":
            print(render_plan(plan))
            print()
            payload = officer.build_order(store, items, args.service, address["addressId"], args.at or "")
            result = officer.submit_order(payload, dry_run=not args.submit)
            if result.get("dryRun"):
                print("下单参数（dry-run，未提交）：")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

    except McdError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
