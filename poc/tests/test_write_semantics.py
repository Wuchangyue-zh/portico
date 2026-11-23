"""写入语义：幂等、乐观锁、越权拒绝、审计闭环。

写操作是这套系统最容易出事的地方，也是"自动执行不加审批"这个决定唯一的
底气来源——如果幂等和版本控制不可靠，就必须回到人工审批，那产品价值就没了。
"""

from __future__ import annotations

import pytest

from portico_poc.errors import (
    Forbidden,
    IdempotencyConflict,
    ValidationError,
    VersionConflict,
)
from portico_poc.gateway import LocalGateway

NOTE = {"customer_id": "C-1001", "note": "已沟通续约意向，下周提供报价。", "expected_version": 3}


def test_write_succeeds_within_scope(gateway: LocalGateway, actor_south, crm):
    """华南销售改自己负责的客户，直接执行，不需人工审批，但要留审计。"""
    result = gateway.invoke(actor_south, "crm.customer.update_note", NOTE, idempotency_key="idem-1")

    assert result.status == "succeeded"
    assert result.result["new_version"] == 4
    assert crm.note_count("C-1001") == 1

    writes = [e for e in gateway.auditor.events if e.capability_id == "crm.customer.update_note"]
    assert writes and writes[-1].decision == "allow"
    assert writes[-1].resource_id == "C-1001"


def test_replay_with_same_key_produces_no_second_effect(gateway: LocalGateway, actor_south, crm):
    """同幂等键重放：返回首次结果，业务数据只变一次。"""
    first = gateway.invoke(actor_south, "crm.customer.update_note", NOTE, idempotency_key="idem-2")
    second = gateway.invoke(actor_south, "crm.customer.update_note", NOTE, idempotency_key="idem-2")

    assert first.result == second.result
    assert crm.note_count("C-1001") == 1
    assert crm.version_of("C-1001") == 4


def test_same_key_different_payload_is_conflict(gateway: LocalGateway, actor_south):
    """同键不同意图必须拒绝，不能静默返回旧结果——那会掩盖调用方 bug。"""
    gateway.invoke(actor_south, "crm.customer.update_note", NOTE, idempotency_key="idem-3")
    changed = {**NOTE, "note": "完全不同的内容"}

    with pytest.raises(IdempotencyConflict):
        gateway.invoke(actor_south, "crm.customer.update_note", changed, idempotency_key="idem-3")


def test_stale_version_is_conflict(gateway: LocalGateway, actor_south):
    gateWayNote = {**NOTE, "expected_version": 1}
    with pytest.raises(VersionConflict):
        gateway.invoke(actor_south, "crm.customer.update_note", gateWayNote, idempotency_key="idem-4")


def test_writing_someone_elses_customer_is_forbidden(gateway: LocalGateway, actor_south):
    """C-1002 属于华北销售，华南销售改不了。"""
    payload = {"customer_id": "C-1002", "note": "越权", "expected_version": 1}
    with pytest.raises(Forbidden):
        gateway.invoke(actor_south, "crm.customer.update_note", payload, idempotency_key="idem-5")


def test_unknown_customer_does_not_leak_existence(gateway: LocalGateway, actor_south):
    """不存在的客户与无权限客户返回同样的拒绝，避免枚举客户编号。"""
    messages = []
    for cid in ("C-9999", "C-1002"):
        with pytest.raises(Forbidden) as exc:
            gateway.invoke(
                actor_south,
                "crm.customer.update_note",
                {"customer_id": cid, "note": "x", "expected_version": 1},
                idempotency_key=f"idem-{cid}",
            )
        messages.append(str(exc.value))
    assert messages[0] == messages[1]


def test_write_without_idempotency_key_rejected(gateway: LocalGateway, actor_south):
    """写操作没幂等键直接拒绝，不给"万一他只想试一次"的机会。"""
    with pytest.raises(ValidationError):
        gateway.invoke(actor_south, "crm.customer.update_note", NOTE)


def test_business_rule_violation_is_not_a_permission_error(gateway: LocalGateway, actor_south):
    """业务规则失败（时间跨度过大）要和权限失败区分开，否则 Agent 会误判为越权并停止。"""
    with pytest.raises(ValidationError):
        gateway.invoke(
            actor_south,
            "erp.order.search",
            {"start_date": "2020-01-01", "end_date": "2026-11-22"},
        )
