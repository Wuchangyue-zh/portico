"""Obot 归属头 → Actor 的翻译层测试。

这一层是整个适配器的信任入口。它一旦出错，后面所有鉴权都是装饰品：
如果伪造一个 Header 就能变成任意用户，两层鉴权全白做。
"""

from __future__ import annotations

import pytest

from portico_poc.errors import Forbidden, Unauthorized
from portico_poc.mcp_adapter.attribution import (
    CallerAttribution,
    GroupPolicy,
    IdentityBridge,
    HDR_GATEWAY_SECRET,
    HDR_IDP_GROUPS,
    HDR_USER_ID,
    HDR_USER_NAME,
)

SECRET = "gateway-shared-secret"


def headers(**overrides: str) -> dict[str, str]:
    base = {
        HDR_GATEWAY_SECRET: SECRET,
        HDR_USER_ID: "U-1001",
        HDR_USER_NAME: "华南销售",
        "x-obot-idp-groups": "portico-sales-south,portico-readonly",
    }
    base.update(overrides)
    return base


# ---------- 来源证明 ----------


def test_missing_gateway_secret_is_rejected():
    """没有网关证明头 = 不是从 Obot 来的，直接拒。"""
    h = headers()
    del h[HDR_GATEWAY_SECRET]
    with pytest.raises(Unauthorized) as exc:
        CallerAttribution.from_headers(h, gateway_secret=SECRET)
    assert "网关证明头" in str(exc.value)


def test_wrong_gateway_secret_is_rejected():
    """知道有这道门，但密码不对。"""
    with pytest.raises(Unauthorized):
        CallerAttribution.from_headers(headers(**{HDR_GATEWAY_SECRET: "wrong"}), gateway_secret=SECRET)


def test_missing_user_id_is_rejected():
    """有网关证明但没身份头，说明 Obot 那边没勾 injectObotHeaders——配置错误也要拒。"""
    h = headers()
    del h[HDR_USER_ID]
    with pytest.raises(Unauthorized) as exc:
        CallerAttribution.from_headers(h, gateway_secret=SECRET)
    assert "injectObotHeaders" in str(exc.value)


def test_header_lookup_is_case_insensitive():
    """HTTP 头大小写不敏感，实现必须容忍。"""
    h = {
        HDR_GATEWAY_SECRET: SECRET,
        "X-Obot-User-Id": "U-7777",
        "X-Obot-IDP-Groups": "portico-sales-south",
    }
    attr = CallerAttribution.from_headers(h, gateway_secret=SECRET)
    assert attr.obot_user_id == "U-7777"
    assert attr.groups == frozenset({"portico-sales-south"})


# ---------- 组解析 ----------


def test_groups_parsed_from_csv():
    """Obot 用 CSV(已排序去重) 编码组声明。"""
    attr = CallerAttribution.from_headers(
        headers(**{HDR_IDP_GROUPS: "a,b,c"}), gateway_secret=SECRET
    )
    assert attr.groups == frozenset({"a", "b", "c"})


def test_empty_and_blank_groups():
    for raw in ("", "   ", ","):
        attr = CallerAttribution.from_headers(headers(**{HDR_IDP_GROUPS: raw}), gateway_secret=SECRET)
        assert attr.groups == frozenset()


# ---------- 组 → 权限与范围 ----------


@pytest.fixture
def bridge() -> IdentityBridge:
    return IdentityBridge(
        {
            "portico-sales-south": GroupPolicy(
                permissions=frozenset({"erp.order.read", "crm.customer.write"}),
                scope={"region": frozenset({"south-china"}), "customer_owner": frozenset({"*"})},
            ),
            "portico-readonly": GroupPolicy(
                permissions=frozenset({"erp.order.read"}),
                scope={"customer_owner": frozenset({"*"})},
            ),
        },
        org_id="org-test",
    )


def test_permissions_union_across_groups(bridge: IdentityBridge):
    attr = CallerAttribution.from_headers(
        headers(**{HDR_IDP_GROUPS: "portico-sales-south,portico-readonly"}), gateway_secret=SECRET
    )
    actor = bridge.resolve(attr)
    assert actor.permissions == frozenset({"erp.order.read", "crm.customer.write"})
    assert actor.scope_of("region") == frozenset({"south-china"})


def test_unknown_group_grants_nothing(bridge: IdentityBridge):
    """未知组静默忽略——不能因为不认识就给了默认权限。"""
    attr = CallerAttribution.from_headers(
        headers(**{HDR_IDP_GROUPS: "totally-unknown-group"}), gateway_secret=SECRET
    )
    actor = bridge.resolve(attr)
    assert actor.permissions == frozenset()
    assert actor.scope_of("region") == frozenset()


def test_no_groups_means_no_access(bridge: IdentityBridge):
    attr = CallerAttribution.from_headers(headers(**{HDR_IDP_GROUPS: ""}), gateway_secret=SECRET)
    actor = bridge.resolve(attr)
    assert not actor.permissions
    assert not actor.scope


def test_customer_owner_always_self_not_group(bridge: IdentityBridge):
    """客户归属维度永远是调用人自己，即便组里写了 '*'。"""
    attr = CallerAttribution.from_headers(
        headers(**{HDR_USER_ID: "U-1001", HDR_IDP_GROUPS: "portico-sales-south"}), gateway_secret=SECRET
    )
    actor = bridge.resolve(attr)
    assert actor.scope_of("customer_owner") == frozenset({"U-1001"})


def test_customer_owner_not_invented_when_no_group_grants_it():
    """没有任何组授予 customer_owner 维度时，不能凭空补一个范围出来。"""
    bridge = IdentityBridge(
        {"portico-report": GroupPolicy(permissions=frozenset({"erp.order.read"}), scope={"region": frozenset({"south-china"})})},
        org_id="org-test",
    )
    attr = CallerAttribution.from_headers(
        headers(**{HDR_IDP_GROUPS: "portico-report"}), gateway_secret=SECRET
    )
    actor = bridge.resolve(attr)
    assert actor.scope_of("customer_owner") == frozenset()
    assert "customer_owner" not in actor.scope


def test_two_users_same_group_get_their_own_customers(bridge: IdentityBridge):
    """同一组的两个人，客户维度各自收敛到自己，互不可见。"""
    a = bridge.resolve(
        CallerAttribution.from_headers(
            headers(**{HDR_USER_ID: "U-1001", HDR_IDP_GROUPS: "portico-sales-south"}), gateway_secret=SECRET
        )
    )
    b = bridge.resolve(
        CallerAttribution.from_headers(
            headers(**{HDR_USER_ID: "U-1002", HDR_IDP_GROUPS: "portico-sales-south"}), gateway_secret=SECRET
        )
    )
    assert a.scope_of("customer_owner") == frozenset({"U-1001"})
    assert b.scope_of("customer_owner") == frozenset({"U-1002"})
    assert a.scope_of("customer_owner").isdisjoint(b.scope_of("customer_owner"))
