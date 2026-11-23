"""端到端：模拟 Obot 注入归属头后调用 MCP 工具。

不起真实 HTTP 服务，而是直接构造工具调用所需的上下文，
验证"归属头 → Actor → 策略 → 适配器"整条链在真实参数下成立。
真实 HTTP 层的验证要在装上 Obot 之后做（见 poc/README.md 的未验证项）。
"""

from __future__ import annotations

import json

import pytest

from portico_poc.errors import Forbidden
from portico_poc.mcp_adapter.server import PorticoMcpApp, build_mcp

SECRET = "e2e-secret"


@pytest.fixture
def app() -> PorticoMcpApp:
    return PorticoMcpApp(gateway_secret=SECRET, org_id="org-demo")


def hdr(user: str, groups: str) -> dict[str, str]:
    return {
        "x-portico-gateway-secret": SECRET,
        "x-obot-user-id": user,
        "x-obot-user-name": user,
        "x-obot-idp-groups": groups,
    }


def test_two_sales_see_different_orders(app: PorticoMcpApp):
    south = app.actor_from_headers(hdr("U-1001", "portico-sales-south"))
    north = app.actor_from_headers(hdr("U-1002", "portico-sales-north"))

    params = {"start_date": "2026-11-01", "end_date": "2026-11-22"}
    rows_s = app.call(south, "erp.order.search", params)["rows"]
    rows_n = app.call(north, "erp.order.search", params)["rows"]

    assert {r["region"] for r in rows_s} == {"south-china"}
    assert {r["region"] for r in rows_n} == {"north-china"}
    assert {r["order_id"] for r in rows_s}.isdisjoint({r["order_id"] for r in rows_n})


def test_cross_region_request_denied(app: PorticoMcpApp):
    south = app.actor_from_headers(hdr("U-1001", "portico-sales-south"))
    with pytest.raises(Forbidden):
        app.call(
            south,
            "erp.order.search",
            {"start_date": "2026-11-01", "end_date": "2026-11-22", "region": "north-china"},
        )


def test_readonly_group_cannot_write(app: PorticoMcpApp):
    reader = app.actor_from_headers(hdr("U-3001", "portico-readonly"))
    with pytest.raises(Forbidden):
        app.call(
            reader,
            "crm.customer.update_note",
            {"customer_id": "C-1001", "note": "x", "expected_version": 3},
            idempotency_key="k",
        )


def test_capability_listing_respects_groups(app: PorticoMcpApp):
    reader = app.actor_from_headers(hdr("U-3001", "portico-readonly"))
    visible = {c["capability_id"] for c in app.list_capabilities(reader)}
    assert "erp.order.search" in visible
    assert "crm.customer.update_note" not in visible


def test_permission_without_scope_dimension_hides_capability(app: PorticoMcpApp):
    """有权限点但缺数据维度 → 能力不出现。fail-closed 的正面验证。

    这不是故障：一个"有 erp.order.read 但没有任何区域范围"的人，
    如果能力可见，他调用时就会拿到全量数据。
    """
    from portico_poc.mcp_adapter.attribution import GroupPolicy

    app.bridge._group_policies["portico-noscope"] = GroupPolicy(  # noqa: SLF001
        permissions=frozenset({"erp.order.read"}),
        scope={},
    )
    actor = app.actor_from_headers(hdr("U-4001", "portico-noscope"))

    assert app.list_capabilities(actor) == []
    with pytest.raises(Forbidden):
        app.call(actor, "erp.order.search", {"start_date": "2026-11-01", "end_date": "2026-11-22"})


def test_write_succeeds_for_owner_group(app: PorticoMcpApp):
    """U-1001 拥有 C-1001，可以写自己的客户。"""
    sales = app.actor_from_headers(hdr("U-1001", "portico-sales-south"))
    result = app.call(
        sales,
        "crm.customer.update_note",
        {"customer_id": "C-1001", "note": "跟进完成", "expected_version": 3},
        idempotency_key="mcp-k1",
    )
    assert result["new_version"] == 4


def test_writing_other_users_customer_is_denied(app: PorticoMcpApp):
    """U-1001 想改 U-1002 的客户，即便同在销售体系也不行。"""
    sales = app.actor_from_headers(hdr("U-1001", "portico-sales-north"))
    with pytest.raises(Forbidden):
        app.call(
            sales,
            "crm.customer.update_note",
            {"customer_id": "C-1002", "note": "越权", "expected_version": 1},
            idempotency_key="mcp-k2",
        )


def test_missing_secret_env_fails_loudly(monkeypatch):
    """没配密钥就启动 = 任何人都能伪造身份，必须拒绝启动而不是默认放行。"""
    from portico_poc.mcp_adapter.server import create_app_from_env

    monkeypatch.delenv("PORTICO_GATEWAY_SECRET", raising=False)
    with pytest.raises(SystemExit):
        create_app_from_env()


def test_mcp_tools_are_registered(app: PorticoMcpApp):
    mcp = build_mcp(app)
    # FastMCP 内部以 tool manager 保存；这里只断言工具名存在
    names = set(mcp._tool_manager._tools.keys())  # noqa: SLF001
    assert {"list_capabilities", "erp_order_search", "crm_customer_update_note"} <= names
