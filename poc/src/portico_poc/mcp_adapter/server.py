"""Portico 的 MCP Server —— 挂到 Obot 上的那一层。

部署形态：Obot 里注册为 runtime=remote 的 MCP Server，并勾选 injectObotHeaders
（X-Obot-User-Id、X-Obot-IDP-Groups）。Obot 负责认证员工并把归属头传进来，
本服务负责把组翻译成权限与数据范围，再用既有的两层鉴权调用业务适配器。

为什么不走 Obot 的 OpenAPI hosting（runtime=openapi）：
    源码 apiclient/types/obotheaders.go 中 SupportsObotHeaders() 只对
    remote 与 containerized 返回 true，openapi 运行时拿不到调用人归属。
    这正是本项目所有能力都必须走自研适配器的代码级依据。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .. import policy
from ..adapters.base import AdapterRegistry, ExecutionContext
from ..adapters.mock_crm import MockCrmAdapter
from ..adapters.mock_erp import MockErpAdapter
from ..contract import CapabilityContract, load_contracts, validate_input
from ..errors import Forbidden, PorticoError, ValidationError
from ..identity import Actor
from ..registry import CapabilityRegistry
from .attribution import GroupPolicy, IdentityBridge

REPO_ROOT = Path(__file__).resolve().parents[4]


def _build_group_policies() -> dict[str, GroupPolicy]:
    """IdP 组 → 权限与范围。

    真实部署时这里从企业 IdP 的组定义或配置中心加载；PoC 用固定映射，
    但结构是真的：一个组同时携带权限点和数据维度。
    """
    return {
        "portico-sales-south": GroupPolicy(
            permissions=frozenset({"erp.order.read", "crm.customer.read", "crm.customer.write"}),
            scope={"region": frozenset({"south-china"}), "customer_owner": frozenset({"*"})},
        ),
        "portico-sales-north": GroupPolicy(
            permissions=frozenset({"erp.order.read", "crm.customer.read", "crm.customer.write"}),
            scope={"region": frozenset({"north-china"}), "customer_owner": frozenset({"*"})},
        ),
        "portico-readonly": GroupPolicy(
            permissions=frozenset({"erp.order.read"}),
            # 必须声明 region：erp.order.search 契约要求该维度，
            # 缺了它能力会被 fail-closed 隐藏——这是设计如此，不是 bug
            scope={"region": frozenset({"south-china"}), "customer_owner": frozenset({"*"})},
        ),
    }


class PorticoMcpApp:
    """把注册中心、适配器、身份桥组装成一个可调用的服务。"""

    def __init__(self, *, gateway_secret: str, org_id: str = "org-demo") -> None:
        if not gateway_secret:
            raise ValueError("必须配置网关共享密钥，否则任何人都能伪造身份头")

        self.gateway_secret = gateway_secret
        self.bridge = IdentityBridge(_build_group_policies(), org_id=org_id)

        self.registry = CapabilityRegistry()
        self.registry.register_all(
            load_contracts(
                [
                    REPO_ROOT / "specs" / "examples" / "erp.order.search.json",
                    REPO_ROOT / "specs" / "examples" / "crm.customer.update_note.json",
                ]
            ),
            publish=True,
        )

        self.erp = MockErpAdapter()
        self.crm = MockCrmAdapter()
        self.adapters = AdapterRegistry()
        self.adapters.register(self.erp)
        self.adapters.register(self.crm)

    # ---- 身份 ----

    def actor_from_headers(self, headers: Any) -> Actor:
        attribution = _attribution_from(headers, self.gateway_secret)
        return self.bridge.resolve(attribution)

    # ---- 能力发现 ----

    def list_capabilities(self, actor: Actor) -> list[dict[str, Any]]:
        return self.registry.discover(actor)

    # ---- 调用 ----

    def call(
        self,
        actor: Actor,
        capability_id: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        contract = self.registry.latest(capability_id)
        if contract is None or not self.registry.can_discover(actor, contract):
            # 与"不存在"同一文案，不给目录枚举留口子
            raise Forbidden("能力不存在或无权访问")

        decision = policy.authorize(actor, contract, params)
        if decision.denied:
            raise Forbidden(decision.reason)

        validate_input(contract, params)

        if contract.side_effect != "none" and not idempotency_key:
            raise ValidationError("写操作必须携带幂等键")

        ctx = ExecutionContext(
            actor=actor,
            contract=contract,
            params=decision.effective_params or params,
            data_scope=decision.data_scope,
            idempotency_key=idempotency_key,
            trace_id=f"trc_mcp_{os.urandom(6).hex()}",
            deadline_ms=contract.timeout_ms,
        )
        return self.adapters.get(contract.adapter_ref).execute(ctx)


def _attribution_from(headers: Any, gateway_secret: str):
    from .attribution import CallerAttribution

    return CallerAttribution.from_headers(headers, gateway_secret=gateway_secret)


def build_mcp(app: PorticoMcpApp) -> FastMCP:
    mcp = FastMCP(
        name="portico",
        instructions=(
            "企业业务能力网关。可查询订单、更新客户备注。"
            "所有结果都已按调用人的授权范围过滤；不要尝试通过参数扩大可见范围。"
        ),
    )

    def _headers(ctx: Context) -> Any:
        request = getattr(ctx.request_context, "request", None)
        if request is None:
            raise ValidationError("缺少 HTTP 请求上下文，本服务只支持 HTTP 传输")
        return request.headers

    @mcp.tool(name="list_capabilities", description="列出当前调用人有权使用的能力")
    def list_capabilities(ctx: Context) -> str:
        try:
            actor = app.actor_from_headers(_headers(ctx))
            return json.dumps(
                {"capabilities": app.list_capabilities(actor)}, ensure_ascii=False, indent=2
            )
        except PorticoError as exc:
            return json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False)

    @mcp.tool(
        name="erp_order_search",
        description="按时间区间与区域查询销售订单明细。金额为含税人民币口径。",
    )
    def erp_order_search(
        ctx: Context,
        start_date: str,
        end_date: str,
        region: str | None = None,
        status: str | None = None,
        page_size: int = 30,
    ) -> str:
        params: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "page_size": page_size,
        }
        if region is not None:
            params["region"] = region
        if status is not None:
            params["status"] = status
        return _run(app, ctx, "erp.order.search", params)

    @mcp.tool(
        name="crm_customer_update_note",
        description="给指定客户追加一条跟进备注。只能追加，不覆盖历史。需幂等键。",
    )
    def crm_customer_update_note(
        ctx: Context,
        customer_id: str,
        note: str,
        expected_version: int,
        idempotency_key: str,
    ) -> str:
        params = {
            "customer_id": customer_id,
            "note": note,
            "expected_version": expected_version,
        }
        return _run(app, ctx, "crm.customer.update_note", params, idempotency_key=idempotency_key)

    return mcp


def _run(
    app: PorticoMcpApp,
    ctx: Context,
    capability_id: str,
    params: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> str:
    """工具调用的统一收口：错误必须变成结构化的可判断信息，而不是抛栈。

    Agent 依赖 error.code 决定重试还是停手，所以绝不能把异常糊成 500。
    """
    try:
        request = getattr(ctx.request_context, "request", None)
        if request is None:
            raise ValidationError("缺少 HTTP 请求上下文")
        actor = app.actor_from_headers(request.headers)
        payload = app.call(actor, capability_id, params, idempotency_key=idempotency_key)
        return json.dumps({"status": "succeeded", "result": payload}, ensure_ascii=False)
    except PorticoError as exc:
        return json.dumps(
            {"status": "failed", "error": {"code": exc.code, "message": exc.message}},
            ensure_ascii=False,
        )


def create_app_from_env() -> PorticoMcpApp:
    secret = os.environ.get("PORTICO_GATEWAY_SECRET", "")
    if not secret:
        raise SystemExit(
            "未配置 PORTICO_GATEWAY_SECRET。\n"
            "该密钥必须在 Obot 的 MCP Server 配置中作为静态 header 一同设置，\n"
            "用来证明请求确实来自网关——X-Obot-* 归属头本身没有签名。"
        )
    return PorticoMcpApp(gateway_secret=secret, org_id=os.environ.get("PORTICO_ORG_ID", "org-demo"))


def main() -> None:
    app = create_app_from_env()
    mcp = build_mcp(app)
    host = os.environ.get("PORTICO_HOST", "0.0.0.0")
    port = int(os.environ.get("PORTICO_PORT", "8099"))
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
