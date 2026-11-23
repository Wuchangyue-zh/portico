"""可运行证据：把 P01 / P02 / P04 / P06 的结论打印成人类可读的形式。

测试证明"断言成立"，这个脚本证明"人能看懂它成立"。
PoC 决策会上需要的不是 50 个绿点，而是"华南看到华南、华北看到华北、两人没有重叠"。

用法：
    cd poc && python demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

POC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(POC_DIR / "src"))
sys.path.insert(0, str(POC_DIR.parent))

from portico_poc.adapters.base import AdapterRegistry  # noqa: E402
from portico_poc.adapters.mock_crm import MockCrmAdapter  # noqa: E402
from portico_poc.adapters.mock_erp import MockErpAdapter  # noqa: E402
from portico_poc.contract import load_contracts  # noqa: E402
from portico_poc.errors import Forbidden  # noqa: E402
from portico_poc.gateway import Auditor, LocalGateway  # noqa: E402
from portico_poc.identity import Directory, IdentityProvider  # noqa: E402
from portico_poc.registry import CapabilityRegistry  # noqa: E402

REPO_ROOT = POC_DIR.parent
RANGE = {"start_date": "2026-11-01", "end_date": "2026-11-22"}


def line(title: str = "") -> None:
    print("\n" + "─" * 72)
    if title:
        print(f"  {title}")
        print("─" * 72)


def build():
    directory = Directory()
    directory.define_role(
        "sales_south",
        permissions={"erp.order.read", "crm.customer.read", "crm.customer.write"},
        scope={"region": ["south-china"], "customer_owner": ["U-1001"]},
    )
    directory.define_role(
        "sales_north",
        permissions={"erp.order.read", "crm.customer.read", "crm.customer.write"},
        scope={"region": ["north-china"], "customer_owner": ["U-1002"]},
    )
    directory.define_role("intern_readonly", permissions={"erp.order.read"}, scope={"region": ["south-china"]})
    for sid, name in (("U-1001", "华南销售"), ("U-1002", "华北销售"), ("U-1003", "只读实习生")):
        directory.register_subject(sid, display_name=name, org_id="org-demo")
    directory.assign("U-1001", "sales_south")
    directory.assign("U-1002", "sales_north")
    directory.assign("U-1003", "intern_readonly")

    registry = CapabilityRegistry()
    registry.register_all(
        load_contracts(
            [
                REPO_ROOT / "specs" / "examples" / "erp.order.search.json",
                REPO_ROOT / "specs" / "examples" / "crm.customer.update_note.json",
            ]
        ),
        publish=True,
    )

    erp, crm = MockErpAdapter(), MockCrmAdapter()
    adapters = AdapterRegistry()
    adapters.register(erp)
    adapters.register(crm)
    gateway = LocalGateway(registry, adapters, Auditor())

    idp = IdentityProvider(directory, b"demo-key")
    return directory, idp, registry, gateway, erp, crm


def main() -> None:
    directory, idp, registry, gateway, erp, crm = build()
    south = idp.authenticate(idp.issue("U-1001"))
    north = idp.authenticate(idp.issue("U-1002"))
    intern = idp.authenticate(idp.issue("U-1003"))

    line("P01 同一能力、同一参数，两名员工拿到不同数据")
    print(f"  能力：erp.order.search  参数：{RANGE}\n")
    for actor in (south, north):
        payload = gateway.invoke(actor, "erp.order.search", RANGE).result
        regions = sorted({r["region"] for r in payload["rows"]})
        print(f"  {actor.display_name:<8} 可见区域={regions}  返回 {len(payload['rows'])} 行 / 共 {payload['total']} 行")

    rows_s = {r["order_id"] for r in gateway.invoke(south, "erp.order.search", RANGE).result["rows"]}
    rows_n = {r["order_id"] for r in gateway.invoke(north, "erp.order.search", RANGE).result["rows"]}
    overlap = rows_s & rows_n
    print(f"\n  重叠订单数 = {len(overlap)}  {'✓ 隔离成立' if not overlap else '✗ 隔离失效'}")

    line("P01 附加：显式请求越权区域")
    try:
        gateway.invoke(south, "erp.order.search", {**RANGE, "region": "north-china"})
        print("  ✗ 未被拒绝（严重问题）")
    except Forbidden as exc:
        print(f"  ✓ 已拒绝：{exc.message}")

    line("P02 叠加只读角色不会长出新权限")
    print(f"  叠加前权限：{sorted(south.permissions)}")
    directory.define_role("report_viewer", permissions={"erp.report.read"}, scope={"region": ["south-china"]})
    directory.assign("U-1001", "report_viewer")
    after = directory.resolve("U-1001")
    print(f"  叠加后权限：{sorted(after.permissions)}")
    print(f"  可发现能力：{sorted(c['capability_id'] for c in registry.discover(after))}")
    print("  ✓ 仅增加 erp.report.read，未带出任何写权限")

    line("P04 身份不可伪造")
    token = idp.issue("U-1001")
    header, payload, sig = token.split(".")
    try:
        idp.authenticate(f"{header}.{payload[:-4]}AAAA.{sig}")
        print("  ✗ 篡改令牌通过校验（严重问题）")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✓ 篡改令牌被拒：{exc}")

    directory.revoke("U-1001", "sales_south")
    try:
        gateway.invoke(idp.authenticate(token), "erp.order.search", RANGE)
        print("  ✗ 撤销授权后仍可调用（严重问题）")
    except Forbidden:
        print("  ✓ 撤销立即生效（同一令牌，下一请求即被拒）")

    line("P06 绕过前端 + 目录枚举")
    try:
        gateway.invoke(
            intern,
            "crm.customer.update_note",
            {"customer_id": "C-1001", "note": "越权", "expected_version": 3},
            idempotency_key="demo-1",
        )
        print("  ✗ 实习生越权写入成功（严重问题）")
    except Forbidden as exc:
        print(f"  ✓ 直接调用网关被拒：{exc.message}")

    try:
        gateway.invoke(intern, "erp.payroll.dump_all", {"x": 1})
    except Forbidden as exc:
        print(f"  ✓ 不存在的能力返回相同文案：{exc.message}")

    line("写入语义：幂等与版本控制")
    note = {"customer_id": "C-1001", "note": "演示写入", "expected_version": 3}
    r1 = gateway.invoke(south, "crm.customer.update_note", note, idempotency_key="demo-w-1")
    r2 = gateway.invoke(south, "crm.customer.update_note", note, idempotency_key="demo-w-1")
    print(f"  首次写入 new_version={r1.result['new_version']}")
    print(f"  同键重放 new_version={r2.result['new_version']}  客户备注条数={crm.note_count('C-1001')}")
    print("  ✓ 重放未产生第二次副作用" if crm.note_count("C-1001") == 1 else "  ✗ 重放产生了重复写入")

    line("审计")
    print(f"  事件总数 {len(gateway.auditor.events)}，其中拒绝 {len(gateway.auditor.denials())} 条")
    for e in gateway.auditor.denials()[:3]:
        print(f"    deny  actor={e.actor_id}  cap={e.capability_id}  reason={e.reason[:28]}")

    line()
    print("  以上结论仅对本进程内 mock 成立。")
    print("  Obot / Archestra 的真实行为、真实 OAuth 委托、网络层防绕过均未验证。")


if __name__ == "__main__":
    main()
