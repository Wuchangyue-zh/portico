"""把 Obot 注入的调用人归属头翻译成 Portico 的 Actor。

这是整个适配器最关键的一层，因为它承接的是**唯一**可信的身份来源。

Obot 对 remote / containerized 类型的 MCP Server 会注入这些头（源码
apiclient/types/obotheaders.go 与 pkg/mcp/attribution/headers.go）：

    X-Obot-User-Id         Obot 内部用户 ID
    X-Obot-User-Name       Obot 用户名
    X-Obot-User-Email      邮箱
    X-Obot-IDP-User-Id     企业 IdP 的主体 ID
    X-Obot-IDP-Groups      IdP 组声明，CSV、已排序去重
    X-Obot-Api-Key-Id      调用所用 API Key 的 ID
    X-Obot-Api-Key-Name    脱敏后的 Key 名

**但这些头没有签名。** 源码里 attribution 包只有 Strip（剥掉客户端自带的同名头）
和 ForCaller（从已验签的 caller 派生），没有任何 HMAC。也就是说：

    能直连到本适配器的人，可以随意伪造 X-Obot-User-Id。

所以适配器必须再加一道"确实来自网关"的证明。这里用一个共享密钥头，
它由 Obot 的 MCP Server 配置里的静态 header 携带。这只是纵深防御的一层，
**真正的边界是网络隔离**：适配器只能被网关访问到。

对应能力契约里的 actor_propagation.bypass_protection 字段。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..errors import Unauthorized

# Obot 的归属头（小写，HTTP 头不区分大小写）
HDR_USER_ID = "x-obot-user-id"
HDR_USER_NAME = "x-obot-user-name"
HDR_USER_EMAIL = "x-obot-user-email"
HDR_IDP_USER_ID = "x-obot-idp-user-id"
HDR_IDP_GROUPS = "x-obot-idp-groups"

# 本适配器自己要求网关携带的证明头
HDR_GATEWAY_SECRET = "x-portico-gateway-secret"


def _get(headers: Mapping[str, str], name: str) -> str:
    """HTTP 头大小写不敏感。传入的既可能是 Starlette Headers 也可能是普通 dict。"""
    try:
        value = headers.get(name)
    except AttributeError:  # pragma: no cover - 防御性
        value = None
    if value is not None:
        return str(value)
    lower = name.lower()
    for key, val in headers.items():
        if str(key).lower() == lower:
            return str(val)
    return ""


def _parse_groups(raw: str) -> frozenset[str]:
    """Obot 用 CSV 编码（csv.Writer，已排序去重）。空值返回空集合。"""
    if not raw.strip():
        return frozenset()
    rows = list(csv.reader([raw]))
    if not rows:
        return frozenset()
    return frozenset(g.strip() for g in rows[0] if g.strip())


@dataclass(frozen=True)
class CallerAttribution:
    """网关确认过的调用人归属。构造这个对象的唯一途径是 from_headers()。"""

    obot_user_id: str
    user_name: str
    email: str
    idp_user_id: str
    groups: frozenset[str]

    @classmethod
    def from_headers(
        cls,
        headers: Mapping[str, str],
        *,
        gateway_secret: str,
    ) -> "CallerAttribution":
        """校验来源后解析归属。任何一步失败都拒绝，不降级。"""
        presented = _get(headers, HDR_GATEWAY_SECRET)
        if not presented:
            raise Unauthorized(
                "缺少网关证明头；本适配器只接受来自 Obot 网关的请求",
                details={"expected_header": HDR_GATEWAY_SECRET},
            )
        # 常数时间比较，避免时序侧信道
        import hmac as _hmac

        if not _hmac.compare_digest(presented, gateway_secret):
            raise Unauthorized("网关证明头不匹配")

        user_id = _get(headers, HDR_USER_ID)
        if not user_id:
            raise Unauthorized(
                "缺少 X-Obot-User-Id；请确认该 MCP Server 在 Obot 中配置了 "
                "injectObotHeaders，且 runtime 为 remote 或 containerized"
            )

        return cls(
            obot_user_id=user_id,
            user_name=_get(headers, HDR_USER_NAME),
            email=_get(headers, HDR_USER_EMAIL),
            idp_user_id=_get(headers, HDR_IDP_USER_ID),
            groups=_parse_groups(_get(headers, HDR_IDP_GROUPS)),
        )


@dataclass(frozen=True)
class GroupPolicy:
    """一组 IdP 组对应的权限点与数据范围。

    IdP 组是员工身份的自然载体：企业里本来就有"华南销售"这类组，
    把权限挂在组上，比在 Obot 里另外维护一套 Profile 更贴近组织现状，
    也不会出现两套授权体系各说各话。
    """

    permissions: frozenset[str]
    scope: Mapping[str, frozenset[str]]


class IdentityBridge:
    """CallerAttribution → Actor。

    设计要点：
      - 权限与范围**全部**从 IdP 组推导，不读请求里的任何参数
      - 未知组直接忽略（不给权限），而不是猜测
      - 一个组都没匹配上 → 空权限空范围 → 后续 fail-closed 拒绝
      - 归属里的 obot_user_id 就是数据归属键（例如客户的 owner）
    """

    def __init__(
        self,
        group_policies: Mapping[str, GroupPolicy],
        *,
        org_id: str,
    ) -> None:
        self._group_policies = dict(group_policies)
        self._org_id = org_id

    def resolve(self, attribution: CallerAttribution):
        """返回 portico_poc.identity.Actor。延迟导入避免循环依赖。"""
        from ..identity import Actor

        permissions: set[str] = set()
        scope: dict[str, set[str]] = {}

        for group in attribution.groups:
            policy = self._group_policies.get(group)
            if policy is None:
                continue  # 未知组不给任何权限
            permissions |= policy.permissions
            for dim, values in policy.scope.items():
                scope.setdefault(dim, set()).update(values)

        # 客户归属维度由调用人本人身份决定，不由组决定。
        # 关键：只有组里已声明 customer_owner 这个维度时才收紧为本人；
        # 若没有任何组授予该维度，就保持缺失 —— 绝不能因为"是本人"就凭空补一个范围出来。
        if "customer_owner" in scope:
            scope["customer_owner"] = {attribution.obot_user_id}

        return Actor(
            subject_id=attribution.obot_user_id,
            display_name=attribution.user_name or attribution.obot_user_id,
            org_id=self._org_id,
            roles=frozenset(attribution.groups),
            permissions=frozenset(permissions),
            scope={dim: frozenset(vals) for dim, vals in scope.items() if vals},
        )


def describe_expected_headers(inject: Iterable[str] = (HDR_USER_ID, HDR_IDP_GROUPS)) -> dict[str, str]:
    """给部署文档用：告诉运维在 Obot 的 MCP Server 配置里要勾哪些头。"""
    mapping = {
        HDR_USER_ID: "X-Obot-User-Id",
        HDR_USER_NAME: "X-Obot-User-Name",
        HDR_USER_EMAIL: "X-Obot-User-Email",
        HDR_IDP_USER_ID: "X-Obot-IDP-User-Id",
        HDR_IDP_GROUPS: "X-Obot-IDP-Groups",
    }
    return {k: v for k, v in mapping.items() if k in set(inject)}
