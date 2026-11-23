"""能力注册中心。

职责边界很窄，但有一条很容易做错的规则：

    **发现即鉴权。**

能力目录不能"先全量返回，再在调用时拦"，因为元数据本身就是信息
（能力名、字段名、枚举值足以还原业务结构）。未授权的能力必须从目录里消失，
而不是召唤时被拒。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .contract import CapabilityContract, validate_contract
from .errors import InvalidContract
from .identity import Actor


@dataclass(frozen=True)
class RegistryEntry:
    contract: CapabilityContract
    adapter_ready: bool = True


class CapabilityRegistry:
    def __init__(self) -> None:
        # (capability_id, version) -> entry
        self._entries: dict[tuple[str, str], RegistryEntry] = {}

    def register(self, contract: CapabilityContract, *, adapter_ready: bool = True) -> None:
        """注册并校验。同一 id+version 不允许重复注册——变更必须升版本。"""
        validate_contract(contract.raw)
        key = (contract.capability_id, contract.version)
        if key in self._entries:
            raise InvalidContract(
                f"能力 {contract.capability_id}@{contract.version} 已注册；"
                "修改契约必须提升版本号，不能覆盖已发布版本"
            )
        self._entries[key] = RegistryEntry(contract=contract, adapter_ready=adapter_ready)

    def register_all(self, contracts: Iterable[CapabilityContract], *, publish: bool = False) -> None:
        for contract in contracts:
            self.register(contract.with_status("published") if publish else contract)

    def versions(self, capability_id: str) -> list[CapabilityContract]:
        return [
            entry.contract
            for (cid, _), entry in sorted(self._entries.items())
            if cid == capability_id
        ]

    def latest(self, capability_id: str) -> CapabilityContract | None:
        candidates = self.versions(capability_id)
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.version_tuple)

    def get(self, capability_id: str, version: str) -> CapabilityContract | None:
        entry = self._entries.get((capability_id, version))
        return entry.contract if entry else None

    # ---- 鉴权：发现与调用共用同一判定 ----

    def can_discover(self, actor: Actor, contract: CapabilityContract) -> bool:
        """这个主体能不能"看见"这个能力。看不见 = 不能调。"""
        if contract.status != "published":
            return False

        if not actor.has_permission(contract.platform_permission):
            return False

        if not contract.additional_grants <= actor.permissions:
            return False

        # fail-closed：声明需要数据范围，但主体在任何维度上都是空范围 → 不暴露。
        # 少了这条，一个"有权限点但没有数据归属"的主体就会看到能力并以全量范围调用。
        if contract.requires_data_scope:
            if not any(actor.scope_of(dim) for dim in contract.scope_dimensions):
                return False

        return True

    def discover(self, actor: Actor) -> list[dict[str, Any]]:
        """返回模型可见的能力列表。只含已授权且已发布的。"""
        visible: list[dict[str, Any]] = []
        for contract in self.versions_for_actor(actor):
            visible.append(contract.summary())
        return visible

    def versions_for_actor(self, actor: Actor) -> list[CapabilityContract]:
        """每个 capability_id 只取对当前主体可见的最高版本。"""
        best: dict[str, CapabilityContract] = {}
        for (cid, _), entry in self._entries.items():
            if not self.can_discover(actor, entry.contract):
                continue
            current = best.get(cid)
            if current is None or entry.contract.version_tuple > current.version_tuple:
                best[cid] = entry.contract
        return [best[cid] for cid in sorted(best)]
