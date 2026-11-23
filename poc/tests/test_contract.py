"""契约层测试：schema 能不能真的拦住错误声明。

只测试"正确的东西能通过"是没用的——真正的价值在于拦住那些看起来合理、
实际上会让权限体系失效的声明。下面每一条都对应一个曾经被写松过的角度。
"""

from __future__ import annotations

import copy
import json

import pytest

from portico_poc.contract import CapabilityContract, load_contract, validate_contract
from portico_poc.errors import InvalidContract
from portico_poc.registry import CapabilityRegistry

from .conftest import REPO_ROOT

EXAMPLES = REPO_ROOT / "specs" / "examples"


def _base() -> dict:
    return json.loads((EXAMPLES / "crm.customer.update_note.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["erp.order.search.json", "crm.customer.update_note.json"])
def test_shipped_examples_are_valid(name: str):
    load_contract(EXAMPLES / name)


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("纯读能力声明了副作用", lambda d: d.update(risk_level="read_only", side_effect="idempotent_write")),
        ("写入但没声明幂等", lambda d: d.pop("idempotency")),
        ("不可幂等写入却不是高影响", lambda d: d.update(side_effect="non_idempotent_write")),
        ("数据范围允许 fail-open", lambda d: d["data_scope"].update(fail_closed=False)),
        ("无身份语义却处理敏感数据", lambda d: d.update(actor_propagation={"mode": "none"})),
        ("可信 Header 没说明防绕过", lambda d: d.update(actor_propagation={"mode": "trusted_gateway_header", "header_name": "X-User"})),
        ("deprecated 却没给迁移路径", lambda d: d.update(status="deprecated")),
        ("capability_id 大小写混乱", lambda d: d.update(capability_id="CRM.Customer.Update")),
        ("缺业务责任人联系方式", lambda d: d["owner"].pop("contact")),
        ("字段名拼写错误", lambda d: d.update(data_classifcation="internal")),
        ("写入不记录最终状态", lambda d: d["audit_policy"].update(record_status=False)),
        ("版本号不是 SemVer", lambda d: d.update(version="1.0")),
        ("超时超过上限", lambda d: d.update(timeout_ms=999_999)),
    ],
)
def test_invalid_documents_are_rejected(case: str, mutate):
    data = copy.deepcopy(_base())
    mutate(data)
    with pytest.raises(InvalidContract):
        validate_contract(data)


def test_error_message_points_at_the_field():
    """报错必须指到具体字段，否则契约评审会变成猜谜。"""
    data = copy.deepcopy(_base())
    data["data_scope"]["fail_closed"] = False

    with pytest.raises(InvalidContract) as exc:
        validate_contract(data)

    violations = exc.value.details["violations"]
    assert any("data_scope" in v["path"] for v in violations)


def test_registry_rejects_silent_overwrite():
    """同一 id+version 不能覆盖注册——改契约必须升版本，否则正在调用的 Agent 会突然换语义。"""
    registry = CapabilityRegistry()
    contract = load_contract(EXAMPLES / "erp.order.search.json")
    registry.register(contract)

    with pytest.raises(InvalidContract):
        registry.register(contract)


def test_with_status_keeps_contract_immutable():
    original = load_contract(EXAMPLES / "erp.order.search.json")
    published = original.with_status("published")

    assert original.status == "draft"
    assert published.status == "published"
    assert isinstance(published, CapabilityContract)
