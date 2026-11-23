"""P02 阻断项：多角色/多 Profile 叠加不能意外放宽权限。

Obot 的 Profile 是取并集的。并集本身不是 bug，但"多给一个只读角色，
结果把写权限也带出来了"就是灾难。这里把守卫写成显式断言。
"""

from __future__ import annotations

import pytest

from portico_poc.errors import Forbidden
from portico_poc.gateway import LocalGateway
from portico_poc.identity import Directory, IdentityProvider

from .conftest import NOV_RANGE, SECRET


def test_union_does_not_conjure_missing_permissions(directory: Directory, actor_south):
    """叠加角色后，有效权限仍是显式授予的并集，不会"长"出没人给过的权限。"""
    before = set(actor_south.permissions)
    assert "crm.customer.delete" not in before

    directory.assign("U-1001", "report_viewer")
    after = directory.resolve("U-1001")

    assert set(after.permissions) == before | {"erp.report.read"}
    assert "crm.customer.delete" not in after.permissions


def test_adding_readonly_role_keeps_write_capability_unchanged(directory: Directory, registry, actor_south):
    """加一个只读角色，可发现的能力集合不变（没有能力用到那个新权限点）。"""
    visible_before = {c["capability_id"] for c in registry.discover(actor_south)}
    directory.assign("U-1001", "report_viewer")
    actor_after = directory.resolve("U-1001")
    visible_after = {c["capability_id"] for c in registry.discover(actor_after)}

    assert visible_before == visible_after


def test_write_capability_requires_all_additional_grants(directory: Directory, registry):
    """additional_grants 是合取：缺任一权限点，写能力就不可见。

    用来防止"有一个通用 write 权限点就横着走"的宽授权设计。
    """
    directory.define_role("crm_write_only", permissions={"crm.customer.write"})
    directory.register_subject("U-2001", display_name="只有写权限的人", org_id="org-demo")
    directory.assign("U-2001", "crm_write_only")

    actor = directory.resolve("U-2001")
    visible = {c["capability_id"] for c in registry.discover(actor)}

    # crm.customer.update_note 还要求 crm.customer.read，此人没有，所以不可见
    assert "crm.customer.update_note" not in visible


def test_scope_union_across_roles_widens_only_where_granted(directory: Directory, gateway: LocalGateway, actor_south):
    """数据范围并集只在被显式授权的维度上扩展，不会漫延到其他维度。"""
    directory.define_role(
        "south_plus_overseas",
        permissions={"erp.order.read"},
        scope={"region": ["overseas"], "customer_owner": ["U-9999"]},
    )
    directory.assign("U-1001", "south_plus_overseas")
    actor = directory.resolve("U-1001")

    assert actor.scope_of("region") == frozenset({"south-china", "overseas"})

    rows = gateway.invoke(actor, "erp.order.search", NOV_RANGE).result["rows"]
    assert {r["region"] for r in rows} == {"south-china", "overseas"}

    # 客户归属维度也被扩展了，但那是显式授予的，属于预期行为
    assert actor.scope_of("customer_owner") == frozenset({"U-1001", "U-9999"})


def test_no_scope_means_no_discovery(directory: Directory, registry):
    """有权限点但没有任何数据范围，必须看不见能力（fail-closed）。"""
    directory.define_role("naked_reader", permissions={"erp.order.read"}, scope={})
    directory.register_subject("U-3001", display_name="无数据归属", org_id="org-demo")
    directory.assign("U-3001", "naked_reader")

    actor = directory.resolve("U-3001")
    assert "erp.order.search" not in {c["capability_id"] for c in registry.discover(actor)}
