"""Mock CRM 适配器 —— 模拟"源系统支持用户级权限"的较理想场景。

与 ERP 相反，这里 CRM 自己按客户归属校验写入权（契约里
data_scope.enforced_by = source_system），适配器只做转发与幂等。

放在同一个 PoC 里，是为了证明两件事能共存：
    - 能拿到用户级委托的系统，就乖乖走源系统鉴权，不要抢它的活
    - 拿不到的（ERP），适配器才补位，且补位要接受更严的评审与测试

顺带把写入最难的三件事跑通：幂等、乐观锁、越权拒绝。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..errors import Forbidden, IdempotencyConflict, ValidationError, VersionConflict
from .base import ExecutionContext


@dataclass
class Customer:
    customer_id: str
    name: str
    owner: str
    version: int = 1
    notes: list[dict[str, Any]] = field(default_factory=list)


def _params_fingerprint(params: dict[str, Any]) -> str:
    """幂等键冲突检测用：同一键对应不同业务意图时必须拒绝，不能静默返回旧结果。"""
    encoded = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MockCrmAdapter:
    ref = "adapter://crm/rest-v1"

    # 客户不存在与越权访问必须返回完全相同的文案。
    # 一旦措辞有差异，攻击者就能用报错内容枚举出哪些客户编号真实存在。
    _CUSTOMER_HIDDEN = "客户不存在或不在你的负责范围内"

    def __init__(self) -> None:
        self._customers: dict[str, Customer] = {
            "C-1001": Customer("C-1001", "华南制造", owner="U-1001", version=3),
            "C-1002": Customer("C-1002", "北方物流", owner="U-1002", version=1),
            "C-1003": Customer("C-1003", "海外贸易", owner="U-1001", version=5),
        }
        # 幂等流水：key -> (参数指纹, 返回结果)
        self._idempotency: dict[str, tuple[str, dict[str, Any]]] = {}

    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        params = dict(ctx.params)

        key = ctx.idempotency_key
        if not key:
            raise ValidationError("写操作必须携带幂等键（Idempotency-Key）")

        fingerprint = _params_fingerprint(params)
        if key in self._idempotency:
            stored_fp, stored_result = self._idempotency[key]
            if stored_fp != fingerprint:
                raise IdempotencyConflict(
                    "同一幂等键提交了不同参数，已拒绝；请使用新的幂等键发起新指令"
                )
            # 重放：返回首次结果，不产生第二次业务副作用
            return stored_result

        customer_id = str(params["customer_id"])
        customer = self._customers.get(customer_id)
        if customer is None:
            # 用 FORBIDDEN 而不是 NOT_FOUND：不泄露"这个客户 ID 是否存在"，
            # 否则可以拿越权拒绝与否来枚举客户编号。
            raise Forbidden(self._CUSTOMER_HIDDEN)

        # 第二层过滤：归属来自源系统数据，与调用人范围比对
        if not ctx.within_scope("customer_owner", customer.owner):
            raise Forbidden(self._CUSTOMER_HIDDEN)

        expected_version = int(params["expected_version"])
        if customer.version != expected_version:
            raise VersionConflict(
                f"客户记录已被修改（当前版本 {customer.version}，你提交的是 {expected_version}）；"
                "请重新读取后再提交"
            )

        note = str(params["note"])
        note_id = f"N-{customer.customer_id}-{customer.version + 1}"
        customer.notes.append({"note_id": note_id, "text": note, "author": ctx.actor.subject_id})
        customer.version += 1

        result = {
            "customer_id": customer.customer_id,
            "new_version": customer.version,
            "note_id": note_id,
        }
        self._idempotency[key] = (fingerprint, result)
        return result

    # ---- 测试用观测点 ----
    def note_count(self, customer_id: str) -> int:
        return len(self._customers[customer_id].notes)

    def version_of(self, customer_id: str) -> int:
        return self._customers[customer_id].version
