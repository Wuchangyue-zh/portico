"""共享夹具：一套身份目录、一份能力目录、一个网关。

刻意把"两个销售 + 一个无 CRM 权限的实习生"设成固定角色，
后面所有 P0x 测试都用这同一组主体，保证结论之间可比。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
POC_SRC = Path(__file__).resolve().parents[1] / "src"
for path in (str(POC_SRC), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from portico_poc.adapters.base import AdapterRegistry  # noqa: E402
from portico_poc.adapters.mock_crm import MockCrmAdapter  # noqa: E402
from portico_poc.adapters.mock_erp import MockErpAdapter  # noqa: E402
from portico_poc.contract import load_contracts  # noqa: E402
from portico_poc.gateway import Auditor, LocalGateway  # noqa: E402
from portico_poc.identity import Directory, IdentityProvider  # noqa: E402
from portico_poc.registry import CapabilityRegistry  # noqa: E402

SECRET = b"poc-only-not-a-real-key"


@pytest.fixture
def directory() -> Directory:
    d = Directory()

    d.define_role(
        "sales_south",
        permissions={"erp.order.read", "crm.customer.read", "crm.customer.write"},
        scope={"region": ["south-china"], "customer_owner": ["U-1001"]},
    )
    d.define_role(
        "sales_north",
        permissions={"erp.order.read", "crm.customer.read", "crm.customer.write"},
        scope={"region": ["north-china"], "customer_owner": ["U-1002"]},
    )
    d.define_role(
        "intern_readonly",
        permissions={"erp.order.read"},
        scope={"region": ["south-china"]},
    )
    # 只读角色，用于验证"多给一个角色不会意外放宽写权限"
    d.define_role(
        "report_viewer",
        permissions={"erp.report.read"},
        scope={"region": ["south-china"]},
    )

    d.register_subject("U-1001", display_name="华南销售", org_id="org-demo")
    d.register_subject("U-1002", display_name="华北销售", org_id="org-demo")
    d.register_subject("U-1003", display_name="只读实习生", org_id="org-demo")

    d.assign("U-1001", "sales_south")
    d.assign("U-1002", "sales_north")
    d.assign("U-1003", "intern_readonly")
    return d


@pytest.fixture
def idp(directory: Directory) -> IdentityProvider:
    return IdentityProvider(directory, SECRET)


@pytest.fixture
def registry() -> CapabilityRegistry:
    r = CapabilityRegistry()
    r.register_all(
        load_contracts(
            [
                REPO_ROOT / "specs" / "examples" / "erp.order.search.json",
                REPO_ROOT / "specs" / "examples" / "crm.customer.update_note.json",
            ]
        ),
        publish=True,
    )
    return r


@pytest.fixture
def erp() -> MockErpAdapter:
    return MockErpAdapter()


@pytest.fixture
def crm() -> MockCrmAdapter:
    return MockCrmAdapter()


@pytest.fixture
def gateway(registry: CapabilityRegistry, erp: MockErpAdapter, crm: MockCrmAdapter) -> LocalGateway:
    adapters = AdapterRegistry()
    adapters.register(erp)
    adapters.register(crm)
    return LocalGateway(registry, adapters, Auditor())


@pytest.fixture
def actor_south(idp: IdentityProvider):
    return idp.authenticate(idp.issue("U-1001"))


@pytest.fixture
def actor_north(idp: IdentityProvider):
    return idp.authenticate(idp.issue("U-1002"))


@pytest.fixture
def actor_intern(idp: IdentityProvider):
    return idp.authenticate(idp.issue("U-1003"))


NOV_RANGE = {"start_date": "2026-11-01", "end_date": "2026-11-22"}
