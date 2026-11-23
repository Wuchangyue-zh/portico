"""策略判定：工具级授权 + 数据范围收紧。

这是"双层鉴权"的第一层。它回答两个问题，且只回答这两个：

    1. 这个主体能不能调用这个能力？（权限点）
    2. 这个主体在能力声明的每个数据维度上，范围是什么？（用于收紧参数）

它**不回答**"这条订单该不该给他看"——那是第二层，由适配器或源系统执行。
把两层分开是因为：网关不该知道订单的业务规则，业务规则改动也不该逼着网关重新发布。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .contract import CapabilityContract
from .identity import Actor


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    effective_params: Mapping[str, Any] = field(default_factory=dict)
    data_scope: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return not self.allowed


def authorize(actor: Actor, contract: CapabilityContract, params: Mapping[str, Any]) -> PolicyDecision:
    """默认拒绝。任何一条不满足即拒绝，不做"部分放行"。"""
    # ---- 1. 平台权限点 ----
    if not actor.has_permission(contract.platform_permission):
        return PolicyDecision(
            allowed=False,
            reason=f"缺少平台权限点 {contract.platform_permission}",
        )

    missing = contract.additional_grants - actor.permissions
    if missing:
        return PolicyDecision(
            allowed=False,
            reason=f"缺少附加权限点 {', '.join(sorted(missing))}",
        )

    # ---- 2. 数据范围收紧 ----
    effective = dict(params)
    scope: dict[str, frozenset[str]] = {}

    if contract.requires_data_scope:
        for dim in contract.scope_dimensions:
            allowed_values = actor.scope_of(dim)

            if not allowed_values:
                # fail-closed：拿不到范围就拒绝，绝不退化成"全量可见"
                return PolicyDecision(
                    allowed=False,
                    reason=f"无法确定主体在 '{dim}' 维度的数据范围，按 fail-closed 拒绝",
                )

            scope[dim] = allowed_values

            if dim not in params:
                # 未指定则按主体范围限定，由适配器按集合过滤
                continue

            requested = params[dim]
            requested_set = {requested} if isinstance(requested, str) else set(requested)

            if not requested_set <= allowed_values:
                # 显式请求越权范围：拒绝并说明，不做静默改写。
                # 静默改成自己有权的那部分会让用户以为是"查全了但没数据"，
                # 这比报错更危险。
                return PolicyDecision(
                    allowed=False,
                    reason=(
                        f"请求的 {dim}={sorted(requested_set - allowed_values)} "
                        f"超出授权范围 {sorted(allowed_values)}"
                    ),
                )

    return PolicyDecision(
        allowed=True,
        reason="allow",
        effective_params=effective,
        data_scope=scope,
    )
