"""P04 阻断项：调用人身份必须端到端可信，且不可伪造。

这是 Obot 已知最薄弱的环节——OpenAPI Runtime 不支持调用人身份 Header 注入，
所以"员工 A 通过 AI 查数据"很可能退化成"所有人共用 ERP 服务账号"。
本 PoC 的立场是：即便底层只能给服务账号，可信 actor 也必须由网关建立，
并且适配器只能用这个 actor 做过滤。
"""

from __future__ import annotations

import pytest

from portico_poc.adapters.base import ExecutionContext
from portico_poc.errors import Forbidden, Unauthorized
from portico_poc.identity import Directory, IdentityProvider

from .conftest import NOV_RANGE, SECRET


def test_tampered_token_rejected(idp: IdentityProvider):
    """改一个字节的令牌必须被拒——签名是唯一防线。"""
    token = idp.issue("U-1001")
    header, payload, signature = token.split(".")
    forged_payload = payload[:-4] + ("AAAA" if not payload.endswith("AAAA") else "BBBB")
    forged = f"{header}.{forged_payload}.{signature}"

    with pytest.raises(Unauthorized):
        idp.authenticate(forged)


def test_token_signed_with_other_key_rejected(idp: IdentityProvider):
    """用别的密钥签的令牌必须被拒。"""
    other = IdentityProvider(idp._directory, b"attacker-key")  # noqa: SLF001
    forged = other.issue("U-1001")

    with pytest.raises(Unauthorized):
        idp.authenticate(forged)


def test_expired_token_rejected(idp: IdentityProvider):
    token = idp.issue("U-1001", ttl_s=-1)
    with pytest.raises(Unauthorized):
        idp.authenticate(token)


def test_garbage_token_rejected(idp: IdentityProvider):
    for bad in ("", "abc", "a.b", "a.b.c.d"):
        with pytest.raises(Unauthorized):
            idp.authenticate(bad)


def test_revocation_takes_effect_immediately(directory: Directory, idp: IdentityProvider, gateway):
    """BR-004：撤销授权后，下一个请求就必须被拒——不用等令牌过期。

    这依赖一个设计决定：权限不写进令牌，每次从目录解析。
    真实 OIDC 若把权限塞进 claims，就做不到这一点。
    """
    token = idp.issue("U-1001")
    actor_before = idp.authenticate(token)
    gateway.invoke(actor_before, "erp.order.search", NOV_RANGE)

    directory.revoke("U-1001", "sales_south")

    # 同一个令牌，重新认证后权限已消失
    actor_after = idp.authenticate(token)
    assert not actor_after.has_permission("erp.order.read")

    with pytest.raises(Forbidden):
        gateway.invoke(actor_after, "erp.order.search", NOV_RANGE)


def _ctx(actor, *, params: dict, scope: dict[str, frozenset[str]]) -> ExecutionContext:
    """构造一个绕开 schema 校验的执行上下文，专门用来攻击适配器的第二层过滤。"""
    return ExecutionContext(
        actor=actor,
        contract=None,  # 适配器不依赖契约，只依赖 ctx 上的可信字段
        params=params,  # type: ignore[arg-type]
        data_scope=scope,
        idempotency_key=None,
        trace_id="trc_test",
        deadline_ms=5000,
    )


def test_adapter_ignores_spoofed_actor_fields(actor_south, erp):
    """params 里塞伪造身份字段，不改变数据范围——过滤只认 ctx.actor / ctx.data_scope。"""
    ctx = _ctx(
        actor_south,
        params={
            "start_date": "2026-11-01",
            "end_date": "2026-11-22",
            "actor_id": "U-1002",
            "subject_id": "U-1002",
            "tenant_id": "other-org",
        },
        scope={"region": frozenset({"south-china"})},
    )

    payload = erp.execute(ctx)
    assert {r["region"] for r in payload["rows"]} == {"south-china"}


def test_adapter_rejects_spoofed_region_even_if_policy_were_bypassed(actor_south, erp):
    """即便网关策略被绕过，适配器自己也会拒绝越权区域——第二层是最后一道防线。"""
    ctx = _ctx(
        actor_south,
        params={"start_date": "2026-11-01", "end_date": "2026-11-22", "region": "north-china"},
        scope={"region": frozenset({"south-china"})},
    )

    with pytest.raises(Forbidden):
        erp.execute(ctx)


def test_adapter_fails_closed_without_scope(actor_south, erp):
    """适配器拿不到范围时必须拒绝，不能退化成"不过滤"。"""
    ctx = _ctx(
        actor_south,
        params={"start_date": "2026-11-01", "end_date": "2026-11-22"},
        scope={},
    )

    with pytest.raises(Forbidden):
        erp.execute(ctx)


def test_unknown_extra_field_rejected_by_schema(gateway, actor_south):
    """模型生成的多余字段应在 schema 层被拒，而不是被静默忽略。"""
    from portico_poc.errors import ValidationError

    with pytest.raises(ValidationError):
        gateway.invoke(
            actor_south,
            "erp.order.search",
            {**NOV_RANGE, "subject_id": "U-1002"},
        )


def test_token_carries_no_permissions(idp: IdentityProvider):
    """令牌里不应包含任何权限声明——只有身份与有效期。"""
    import base64
    import json

    token = idp.issue("U-1001")
    payload_b64 = token.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))

    assert set(payload) <= {"sub", "org", "iat", "exp"}
