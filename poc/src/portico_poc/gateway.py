"""网关：能力发现与调用的唯一入口。

GatewayPort 是刻意抽出来的接口。真实落地时后面挂的是 Obot，
但 Master Agent 与业务适配器只认这个接口，不认 Obot 的私有 API——
这样换网关不用改业务代码，也不会被某个网关的权限模型绑定。

（这条"不并联、可替换"的决定来自 ADR-002；接口先立住，实现后填。）
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from . import policy
from .adapters.base import AdapterRegistry, ExecutionContext
from .contract import CapabilityContract, validate_input
from .errors import (
    Forbidden,
    PorticoError,
    UpstreamUnavailable,
    ValidationError,
)
from .identity import Actor
from .registry import CapabilityRegistry

# 未授权与不存在返回完全相同的信息：否则攻击者可以用"报错不同"来枚举能力目录
_HIDDEN_MESSAGE = "能力不存在或无权访问"


@dataclass(frozen=True)
class AuditEvent:
    """审计事件。字段集合对齐文档 07.7，但只记录摘要，不落原始业务数据。"""

    event_id: str
    occurred_at: str
    org_id: str
    actor_id: str
    capability_id: str
    operation: str
    resource_id: str | None
    decision: str
    reason: str
    trace_id: str
    result_status: str
    latency_ms: int = 0


class Auditor:
    """内存审计。真实实现要落到不可篡改的存储，且与业务库分离。"""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, **kwargs: Any) -> AuditEvent:
        event = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            **kwargs,
        )
        self.events.append(event)
        return event

    def denials(self) -> list[AuditEvent]:
        return [e for e in self.events if e.decision == "deny"]


@dataclass(frozen=True)
class InvocationResult:
    invocation_id: str
    trace_id: str
    status: str
    capability_id: str
    version: str
    result: Mapping[str, Any]
    policy_decision: str = "allow"


class GatewayPort(Protocol):
    """Master Agent 只依赖这三个方法。"""

    def discover(self, actor: Actor) -> list[dict[str, Any]]: ...

    def invoke(
        self,
        actor: Actor,
        capability_id: str,
        params: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        version: str | None = None,
    ) -> InvocationResult: ...


class LocalGateway:
    """进程内实现，用于在没有 Obot 的情况下把鉴权链跑通并留证。"""

    def __init__(
        self,
        registry: CapabilityRegistry,
        adapters: AdapterRegistry,
        auditor: Auditor | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = adapters
        self.auditor = auditor or Auditor()
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="portico-adapter")

    # ---- 发现 ----
    def discover(self, actor: Actor) -> list[dict[str, Any]]:
        return self._registry.discover(actor)

    # ---- 调用 ----
    def invoke(
        self,
        actor: Actor,
        capability_id: str,
        params: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        version: str | None = None,
    ) -> InvocationResult:
        trace_id = f"trc_{uuid.uuid4().hex[:16]}"
        started = time.monotonic()

        contract = self._resolve(actor, capability_id, version, trace_id)
        decision = self._authorize_or_deny(actor, contract, params, trace_id, started)

        # 先鉴权再校验入参：反过来会把 schema 细节泄露给无权限的调用方
        validate_input(contract, params)

        if contract.side_effect != "none" and not idempotency_key:
            raise ValidationError(
                f"能力 {capability_id} 有业务副作用，必须携带幂等键",
                details={"capability_id": capability_id},
            )

        ctx = ExecutionContext(
            actor=actor,
            contract=contract,
            params=decision.effective_params or params,
            data_scope=decision.data_scope,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            deadline_ms=contract.timeout_ms,
        )

        try:
            adapter = self._adapters.get(contract.adapter_ref)
        except LookupError as exc:
            self._audit(actor, contract, "invoke", params, "deny", str(exc), trace_id, "error", started)
            raise UpstreamUnavailable(str(exc)) from exc

        try:
            payload = self._run_with_timeout(adapter.execute, ctx, contract.timeout_ms)
        except PorticoError as exc:
            # 业务拒绝也要留痕：审计的价值一半在于记录"谁被拒了、为什么"
            self._audit(actor, contract, "invoke", params, "allow", exc.code, trace_id, "failed", started)
            raise

        self._audit(actor, contract, "invoke", params, "allow", "ok", trace_id, "succeeded", started)

        return InvocationResult(
            invocation_id=f"inv_{uuid.uuid4().hex[:12]}",
            trace_id=trace_id,
            status="succeeded",
            capability_id=contract.capability_id,
            version=contract.version,
            result=payload,
        )

    # ---- 内部 ----

    def _resolve(
        self,
        actor: Actor,
        capability_id: str,
        version: str | None,
        trace_id: str,
    ) -> CapabilityContract:
        contract = (
            self._registry.get(capability_id, version)
            if version is not None
            else self._registry.latest(capability_id)
        )
        # 不存在 / 未发布 / 无权限，一律同一个响应，且都要审计
        if contract is None or not self._registry.can_discover(actor, contract):
            self.auditor.record(
                org_id=actor.org_id,
                actor_id=actor.subject_id,
                capability_id=capability_id,
                operation="invoke",
                resource_id=None,
                decision="deny",
                reason="capability_unavailable",
                trace_id=trace_id,
                result_status="rejected",
            )
            raise Forbidden(_HIDDEN_MESSAGE)

        return contract

    def _authorize_or_deny(
        self,
        actor: Actor,
        contract: CapabilityContract,
        params: Mapping[str, Any],
        trace_id: str,
        started: float,
    ) -> policy.PolicyDecision:
        decision = policy.authorize(actor, contract, params)
        if decision.allowed:
            return decision
        self._audit(actor, contract, "invoke", params, "deny", decision.reason, trace_id, "rejected", started)
        raise Forbidden(decision.reason)

    def _run_with_timeout(self, fn: Any, ctx: ExecutionContext, timeout_ms: int) -> dict[str, Any]:
        """超时由网关强制，不依赖适配器自觉。

        已知局限：线程无法被真正杀死，超时后仍可能在后台跑完。
        真实实现应使用进程隔离或异步取消，否则一个慢适配器会持续占用资源。
        """
        future = self._executor.submit(fn, ctx)
        try:
            return future.result(timeout=timeout_ms / 1000)
        except FutureTimeout as exc:
            raise UpstreamUnavailable(
                f"适配器执行超过 {timeout_ms}ms 上限",
                details={"trace_id": ctx.trace_id},
            ) from exc

    def _audit(
        self,
        actor: Actor,
        contract: CapabilityContract,
        operation: str,
        params: Mapping[str, Any],
        decision: str,
        reason: str,
        trace_id: str,
        result_status: str,
        started: float | None = None,
    ) -> None:
        self.auditor.record(
            org_id=actor.org_id,
            actor_id=actor.subject_id,
            capability_id=contract.capability_id,
            operation=operation,
            resource_id=self._resource_id(params),
            decision=decision,
            reason=reason,
            trace_id=trace_id,
            result_status=result_status,
            latency_ms=int((time.monotonic() - started) * 1000) if started else 0,
        )

    @staticmethod
    def _resource_id(params: Mapping[str, Any]) -> str | None:
        for key in ("customer_id", "order_id", "region"):
            if key in params:
                return str(params[key])
        return None
