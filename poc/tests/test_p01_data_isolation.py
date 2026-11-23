"""P01 阻断项：两名员工调用同一能力，只能看到各自范围内的数据。

这是整个项目最核心的断言。如果这条不成立，产品就没有存在意义——
员工会看到一个"能跨系统问数"的假象，然后用它读到不该读的数据。
"""

from __future__ import annotations

from portico_poc.gateway import LocalGateway

from .conftest import NOV_RANGE


def test_two_employees_same_capability_disjoint_data(gateway: LocalGateway, actor_south, actor_north):
    """同一 capability_id、同一参数、同一时间，两人拿到的数据必须互不重叠。"""
    result_south = gateway.invoke(actor_south, "erp.order.search", NOV_RANGE)
    result_north = gateway.invoke(actor_north, "erp.order.search", NOV_RANGE)

    rows_south = result_south.result["rows"]
    rows_north = result_north.result["rows"]

    assert rows_south, "华南销售应该能看到数据，否则说明测试数据构造有误"
    assert rows_north, "华北销售应该能看到数据"

    assert {r["region"] for r in rows_south} == {"south-china"}
    assert {r["region"] for r in rows_north} == {"north-china"}

    ids_south = {r["order_id"] for r in rows_south}
    ids_north = {r["order_id"] for r in rows_north}
    assert ids_south.isdisjoint(ids_north), "两名员工看到了重叠订单，数据隔离失效"


def test_no_access_to_third_region(gateway: LocalGateway, actor_south, actor_north):
    """没有任何人授权的区域（overseas）必须对所有人不可见。"""
    for actor in (actor_south, actor_north):
        rows = gateway.invoke(actor, "erp.order.search", NOV_RANGE).result["rows"]
        assert "overseas" not in {r["region"] for r in rows}


def test_explicit_out_of_scope_region_is_rejected_not_silently_shrunk(gateway: LocalGateway, actor_south):
    """越权显式请求要报错，不能静默缩减。

    静默缩减会让用户以为"查了华北但没数据"，比直接报错危险得多——
    他会拿这个结论去做业务判断。
    """
    import pytest

    from portico_poc.errors import Forbidden

    with pytest.raises(Forbidden) as exc:
        gateway.invoke(actor_south, "erp.order.search", {**NOV_RANGE, "region": "north-china"})

    assert "超出授权范围" in str(exc.value)


def test_scope_is_derived_from_identity_not_from_params(gateway: LocalGateway, actor_south):
    """不传 region 时，范围由身份决定，而不是"不传就等于全量"。"""
    payload = gateway.invoke(actor_south, "erp.order.search", NOV_RANGE).result
    assert {r["region"] for r in payload["rows"]} == {"south-china"}
    assert payload["total"] == len(payload["rows"]) or payload["total"] >= len(payload["rows"])
