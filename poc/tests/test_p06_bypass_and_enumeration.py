"""P06 阻断项：绕过前端也不能越权，且不能靠报错差异枚举能力目录。

两个攻击面：
    1. 拿到网关地址后直接调用，跳过 Portal 的一切前端校验
    2. 靠"报错不同"区分"能力存在但你没权限"和"能力不存在"
"""

from __future__ import annotations

import pytest

from portico_poc.errors import Forbidden
from portico_poc.gateway import LocalGateway

from .conftest import NOV_RANGE


def test_direct_gateway_call_without_permission_is_denied(gateway: LocalGateway, actor_intern):
    """实习生没有 CRM 写权限，直接打网关也必须被拒。"""
    with pytest.raises(Forbidden):
        gateway.invoke(
            actor_intern,
            "crm.customer.update_note",
            {"customer_id": "C-1001", "note": "越权写入尝试", "expected_version": 3},
            idempotency_key="attacker-key-1",
        )


def test_hidden_and_missing_capability_are_indistinguishable(gateway: LocalGateway, actor_intern):
    """「存在但无权」与「不存在」必须返回完全一样的信息，否则可用于枚举目录。"""
    messages: list[str] = []

    # 存在，但实习生无权（无 crm.customer.write）
    with pytest.raises(Forbidden) as existing:
        gateway.invoke(
            actor_intern,
            "crm.customer.update_note",
            {"customer_id": "C-1001", "note": "x", "expected_version": 3},
            idempotency_key="k1",
        )
    messages.append(str(existing.value))

    # 完全不存在的能力
    with pytest.raises(Forbidden) as missing:
        gateway.invoke(actor_intern, "erp.payroll.dump_all", {"anything": 1})
    messages.append(str(missing.value))

    assert messages[0] == messages[1], "报错可区分，等于把能力目录泄露给了无权限用户"


def test_discovery_does_not_leak_unauthorized_capabilities(gateway: LocalGateway, actor_intern):
    """实习生只能看见 ERP 查询，看不见任何 CRM 能力。"""
    visible = {c["capability_id"] for c in gateway.discover(actor_intern)}
    assert "erp.order.search" in visible
    assert not any(cid.startswith("crm.") for cid in visible)


def test_denied_calls_are_audited(gateway: LocalGateway, actor_intern):
    """拒绝必须留痕——审计的一半价值在记录"谁被拒了、为什么"。"""
    with pytest.raises(Forbidden):
        gateway.invoke(actor_intern, "crm.customer.update_note", {"customer_id": "C-1001", "note": "x", "expected_version": 3}, idempotency_key="k2")

    denials = gateway.auditor.denials()
    assert denials, "越权尝试未被记录，出事后无法追溯"
    assert denials[-1].actor_id == "U-1003"
    assert denials[-1].decision == "deny"


def test_unpublished_capability_is_invisible_and_uncallable():
    """draft 状态的能力既不出现也不可调——发布是一道真实的闸门。"""
    from portico_poc.adapters.base import AdapterRegistry
    from portico_poc.adapters.mock_erp import MockErpAdapter
    from portico_poc.contract import load_contract
    from portico_poc.gateway import Auditor, LocalGateway
    from portico_poc.registry import CapabilityRegistry

    from .conftest import REPO_ROOT

    registry = CapabilityRegistry()
    registry.register(
        load_contract(REPO_ROOT / "specs" / "examples" / "erp.order.search.json")
        # 注意：未 publish
    )
    adapters = AdapterRegistry()
    adapters.register(MockErpAdapter())
    gw = LocalGateway(registry, adapters, Auditor())

    from portico_poc.identity import Directory, IdentityProvider

    directory = Directory()
    directory.define_role("r", permissions={"erp.order.read"}, scope={"region": ["south-china"]})
    directory.register_subject("U-1", display_name="t", org_id="o")
    directory.assign("U-1", "r")
    actor = IdentityProvider(directory, b"k").authenticate(IdentityProvider(directory, b"k").issue("U-1"))

    assert gw.discover(actor) == []
    with pytest.raises(Forbidden):
        gw.invoke(actor, "erp.order.search", NOV_RANGE)
