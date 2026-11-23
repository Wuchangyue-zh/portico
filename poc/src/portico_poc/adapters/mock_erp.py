"""Mock ERP 适配器 —— 模拟"源系统只有服务账号、没有用户级委托"的典型遗留系统。

这正是 Obot OpenAPI Runtime 撑不住的那类场景：它能把 REST 接口包装成工具，
但没法把"谁在调用"带到 ERP 里。所以适配器必须自己承担业务鉴权，契约里
actor_propagation.mode 因此写成 service_account_with_actor_validation。

这里刻意做成两层过滤：
    第一层（网关 policy）算出 ctx.data_scope
    第二层（本文件）用 ctx.data_scope 再过滤一次
不是冗余——第一层可能被改错，第二层是最后一道防线，且它离数据最近。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..errors import Forbidden, ValidationError
from .base import ExecutionContext

_MAX_SPAN_DAYS = 90


def _build_orders() -> list[dict[str, Any]]:
    """固定种子生成订单，保证测试可复现。三个区域，其中一个无人有权限。"""
    regions = ["south-china", "north-china", "overseas"]
    statuses = ["paid", "shipped", "created", "cancelled"]
    orders: list[dict[str, Any]] = []
    base_day = date(2026, 11, 1)

    for i in range(90):
        region = regions[i % len(regions)]
        orders.append(
            {
                "order_id": f"ORD-{1000 + i:04d}",
                "order_date": (base_day + timedelta(days=i % 21)).isoformat(),
                "region": region,
                "customer_id": f"C-{2000 + i:04d}",
                "amount_cny": round(1200.0 + (i * 137) % 8800, 2),
                "status": statuses[i % len(statuses)],
                "owner": f"U-{1001 + (i % 3)}",
            }
        )
    return orders


class MockErpAdapter:
    ref = "adapter://erp/rest-v2"

    def __init__(self) -> None:
        self._orders = _build_orders()
        self.upstream_calls = 0

    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        params = ctx.params
        self._check_business_rules(params)

        start = date.fromisoformat(params["start_date"])
        end = date.fromisoformat(params["end_date"])
        status_filter = params.get("status")
        page_size = int(params.get("page_size", 30))

        # ---- 第二层过滤：范围来自可信的 ctx，不来自 params ----
        allowed_regions = ctx.scoped("region")
        if not allowed_regions:
            # 适配器自己也 fail-closed，不指望网关一定记得检查
            raise Forbidden("适配器未能确定调用人的区域范围")

        # 若调用方显式指定了区域，只可能是网关已校验为子集；这里再交一次
        requested_region = params.get("region")
        if requested_region is not None and not ctx.within_scope("region", requested_region):
            raise Forbidden(f"区域 {requested_region} 不在调用人授权范围内")

        self.upstream_calls += 1

        rows = [
            o
            for o in self._orders
            if o["region"] in allowed_regions
            and (requested_region is None or o["region"] == requested_region)
            and start <= date.fromisoformat(o["order_date"]) <= end
            and (status_filter is None or o["status"] == status_filter)
        ]
        rows.sort(key=lambda o: (o["order_date"], o["order_id"]))

        return {
            "rows": rows[:page_size],
            "total": len(rows),
            "amount_scope": "tax_included_cny",
        }

    def _check_business_rules(self, params: dict[str, Any]) -> None:
        """业务规则校验。这些规则无法用 JSON Schema 表达，只能在这里做。"""
        try:
            start = date.fromisoformat(params["start_date"])
            end = date.fromisoformat(params["end_date"])
        except (KeyError, ValueError) as exc:
            raise ValidationError("日期格式应为 YYYY-MM-DD") from exc

        if end < start:
            raise ValidationError("end_date 不能早于 start_date")

        span = (end - start).days
        if span > _MAX_SPAN_DAYS:
            raise ValidationError(
                f"查询跨度 {span} 天超过上限 {_MAX_SPAN_DAYS} 天；请拆分区间或改用汇总能力"
            )
