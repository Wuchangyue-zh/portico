"""适配器接口。

适配器是"第二条鉴权链"的落点。它拿到的 ExecutionContext 里有两个必须被区别对待的字段：

    ctx.actor        —— 网关验过签的主体，可信
    ctx.params       —— 模型生成的参数，不可信
    ctx.data_scope   —— 网关按主体解析出的范围，可信

第二层过滤必须基于 actor / data_scope，**不能**基于 params 里出现的任何身份字段。
这是适配器最容易写错的地方：一旦从 params 读 user_id 做过滤，整个权限体系
就退化成了"模型说什么就是什么"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from ..contract import CapabilityContract
from ..identity import Actor


@dataclass(frozen=True)
class ExecutionContext:
    actor: Actor
    contract: CapabilityContract
    params: Mapping[str, Any]
    data_scope: Mapping[str, frozenset[str]]
    idempotency_key: str | None
    trace_id: str
    deadline_ms: int

    def scoped(self, dimension: str) -> frozenset[str]:
        return self.data_scope.get(dimension, frozenset())

    def within_scope(self, dimension: str, value: str) -> bool:
        """第二层过滤的统一入口。适配器用它，而不是自己比较字符串。"""
        return value in self.scoped(dimension)


@runtime_checkable
class Adapter(Protocol):
    """adapter_ref 必须与能力契约里的 adapter_ref 一致。"""

    ref: str

    def execute(self, ctx: ExecutionContext) -> dict[str, Any]: ...


class AdapterRegistry:
    """adapter_ref -> adapter 实例。真实实现里这是插件发现，这里是显式注册。"""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        if adapter.ref in self._adapters:
            raise ValueError(f"适配器 {adapter.ref} 重复注册")
        self._adapters[adapter.ref] = adapter

    def get(self, ref: str) -> Adapter:
        adapter = self._adapters.get(ref)
        if adapter is None:
            raise LookupError(f"未找到适配器 {ref}（能力契约已注册但执行端缺失）")
        return adapter
