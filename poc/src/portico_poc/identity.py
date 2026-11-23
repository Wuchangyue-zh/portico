"""身份与权限目录。

这里有一个刻意的设计选择，是整个 PoC 最想验证的东西之一：

    **令牌里只放"你是谁"，不放"你能干什么"。**

真实 OIDC 令牌通常把权限声明（claims）塞进 payload，一旦签发就无法收回——
员工离职或授权被撤销后，那个令牌在过期前依然有效。BR-004 要求撤销立即生效，
所以权限与数据范围必须在每次调用时从服务端目录重新解析。

代价是每次调用多一次目录查询（可缓存，但要能被变更事件失效）。
用缓存换合规，这个交换在 MVP 阶段是划算的。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .errors import Unauthorized


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass(frozen=True)
class Role:
    """角色 = 一组权限点 + 一组数据范围。

    数据范围按维度给出：{"region": ("south-china",), "customer_owner": ("U-1001",)}
    维度名必须与能力契约 data_scope.dimensions 里的名字一致，否则网关无法收紧。
    """

    name: str
    permissions: frozenset[str]
    scope: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Actor:
    """一次调用中的可信主体。只可能由 IdentityProvider.authenticate 构造。

    适配器可以读 ctx.actor 来决定过滤条件，但绝不能从 params 里读 actor 相关字段——
    params 是模型生成的，actor 是网关验证过的，两者不可混淆。
    """

    subject_id: str
    display_name: str
    org_id: str
    roles: frozenset[str]
    permissions: frozenset[str]
    scope: Mapping[str, frozenset[str]]

    def scope_of(self, dimension: str) -> frozenset[str]:
        return self.scope.get(dimension, frozenset())

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions


class Directory:
    """组织目录：谁是谁、谁有什么角色、谁被撤销了什么。

    真实实现对接企业 IdP / HR 系统；这里的规则保持一致：
      - 权限取并集（一个员工可能身兼多角色）
      - 数据范围按维度取并集
      - 并集只加不减 —— 所以"多给一个角色不该意外放宽写权限"必须靠测试守住（P02）
    """

    def __init__(self) -> None:
        self._roles: dict[str, Role] = {}
        self._assignments: dict[str, set[str]] = {}
        self._subjects: dict[str, dict[str, str]] = {}

    def define_role(
        self,
        name: str,
        *,
        permissions: Iterable[str],
        scope: Mapping[str, Iterable[str]] | None = None,
    ) -> Role:
        role = Role(
            name=name,
            permissions=frozenset(permissions),
            scope={dim: tuple(sorted(vals)) for dim, vals in (scope or {}).items()},
        )
        self._roles[name] = role
        return role

    def register_subject(self, subject_id: str, *, display_name: str, org_id: str) -> None:
        self._subjects[subject_id] = {"display_name": display_name, "org_id": org_id}

    def assign(self, subject_id: str, *role_names: str) -> None:
        for name in role_names:
            if name not in self._roles:
                raise KeyError(f"未定义的角色：{name}")
        self._assignments.setdefault(subject_id, set()).update(role_names)

    def revoke(self, subject_id: str, *role_names: str) -> None:
        """撤销授权。下一次调用立即生效，因为权限不来自令牌。"""
        held = self._assignments.get(subject_id, set())
        held.difference_update(role_names)

    def resolve(self, subject_id: str) -> Actor:
        identity = self._subjects.get(subject_id)
        if identity is None:
            raise Unauthorized("主体未在组织目录中登记")

        role_names = frozenset(self._assignments.get(subject_id, set()))
        permissions: set[str] = set()
        scope_union: dict[str, set[str]] = {}

        for name in role_names:
            role = self._roles[name]
            permissions |= role.permissions
            for dim, vals in role.scope.items():
                scope_union.setdefault(dim, set()).update(vals)

        return Actor(
            subject_id=subject_id,
            display_name=identity["display_name"],
            org_id=identity["org_id"],
            roles=role_names,
            permissions=frozenset(permissions),
            scope={dim: frozenset(vals) for dim, vals in scope_union.items()},
        )


class IdentityProvider:
    """签令牌、验令牌。

    真实系统用企业 IdP 签发的 OIDC ID Token / Access Token；这里用 HMAC 模拟同样的
    安全性质：签名不可伪造、载荷可读不可改、有有效期。刻意**不做**的事：把权限写进载荷。
    """

    def __init__(self, directory: Directory, secret: bytes) -> None:
        self._directory = directory
        self._secret = secret

    def _sign(self, signing_input: bytes) -> str:
        return _b64e(hmac.new(self._secret, signing_input, hashlib.sha256).digest())

    def issue(self, subject_id: str, *, org_id: str | None = None, ttl_s: int = 900) -> str:
        if subject_id not in self._directory._subjects:  # noqa: SLF001 - mock 内部一致性检查
            raise Unauthorized("主体未在组织目录中登记，无法签发令牌")
        now = int(time.time())
        payload = {
            "sub": subject_id,
            "org": org_id or self._directory._subjects[subject_id]["org_id"],  # noqa: SLF001
            "iat": now,
            "exp": now + ttl_s,
        }
        header_b64 = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        signature = self._sign(f"{header_b64}.{payload_b64}".encode("ascii"))
        return f"{header_b64}.{payload_b64}.{signature}"

    def authenticate(self, token: str) -> Actor:
        """验签 → 查有效期 → 从目录重新解析权限。任何一步不过都拒绝。"""
        if not token or token.count(".") != 2:
            raise Unauthorized("令牌格式非法")

        header_b64, payload_b64, signature = token.split(".")
        expected = self._sign(f"{header_b64}.{payload_b64}".encode("ascii"))

        # 用常数时间比较，避免通过响应时间差逐字节猜签名
        if not hmac.compare_digest(expected, signature):
            raise Unauthorized("令牌签名校验失败")

        try:
            payload = json.loads(_b64d(payload_b64))
        except (ValueError, json.JSONDecodeError) as exc:
            raise Unauthorized("令牌载荷无法解析") from exc

        if not isinstance(payload, dict) or "sub" not in payload:
            raise Unauthorized("令牌缺少主体标识")

        if int(payload.get("exp", 0)) <= int(time.time()):
            raise Unauthorized("令牌已过期")

        # 关键：权限与数据范围在此时才取，令牌里没有
        return self._directory.resolve(str(payload["sub"]))
