"""能力契约的加载与校验。

契约是本项目的核心资产：它是唯一同时被"人"（业务 Owner 评审口径）、
"模型"（检索与选工具）、"网关"（鉴权与收紧参数）、"适配器"（执行与过滤）
读到的同一份东西。所以它必须在注册时就校验，而不是运行期碰运气。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import jsonschema

from .errors import InvalidContract, ValidationError

_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _schema_path() -> Path:
    override = os.environ.get("PORTICO_CAPABILITY_SCHEMA")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "specs" / "capability.schema.json"


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@dataclass(frozen=True)
class CapabilityContract:
    """一份校验过的能力契约。内部保留原始 dict，对外暴露语义化访问器。

    不拆成几十个字段的 dataclass，是因为契约会随版本演进加字段，
    而消费方真正需要的只是少数几个判断（能不能被发现、要不要收紧数据范围）。
    """

    raw: Mapping[str, Any]

    # ---- 标识 ----
    @property
    def capability_id(self) -> str:
        return self.raw["capability_id"]

    @property
    def version(self) -> str:
        return self.raw["version"]

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return tuple(int(p) for p in self.version.split("."))  # type: ignore[return-value]

    @property
    def status(self) -> str:
        return self.raw.get("status", "draft")

    @property
    def display_name(self) -> str:
        return self.raw["display_name"]

    @property
    def description(self) -> str:
        return self.raw["description"]

    # ---- 第一层：网关鉴权 ----
    @property
    def platform_permission(self) -> str:
        return self.raw["permission"]["platform_permission"]

    @property
    def additional_grants(self) -> frozenset[str]:
        return frozenset(self.raw["permission"].get("additional_grants", ()))

    @property
    def requires_data_scope(self) -> bool:
        return bool(self.raw["permission"]["requires_data_scope"])

    # ---- 第二层：数据范围 ----
    @property
    def scope_dimensions(self) -> tuple[str, ...]:
        return tuple(self.raw["data_scope"]["dimensions"])

    @property
    def scope_enforced_by(self) -> str:
        return self.raw["data_scope"]["enforced_by"]

    # ---- 执行 ----
    @property
    def adapter_ref(self) -> str:
        return self.raw["adapter_ref"]

    @property
    def timeout_ms(self) -> int:
        return int(self.raw["timeout_ms"])

    @property
    def risk_level(self) -> str:
        return self.raw["risk_level"]

    @property
    def side_effect(self) -> str:
        return self.raw["side_effect"]

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return self.raw["input_schema"]

    @property
    def audit_level(self) -> str:
        return self.raw["audit_policy"]["level"]

    @property
    def data_classification(self) -> str:
        return self.raw["data_classification"]

    # ---- 变更 ----
    def with_status(self, status: str) -> "CapabilityContract":
        """返回改了状态的副本。PoC 里用它在测试中把 draft 提升为 published。"""
        updated = dict(self.raw)
        updated["status"] = status
        return CapabilityContract(updated)

    def summary(self) -> dict[str, Any]:
        """发现接口返回给模型的东西：只要够选工具，不要塞完整契约。"""
        return {
            "capability_id": self.capability_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "business_domain": self.raw["business_domain"],
            "risk_level": self.risk_level,
            "side_effect": self.side_effect,
            "input_schema": self.input_schema,
        }


def validate_contract(data: Mapping[str, Any]) -> None:
    """按 specs/capability.schema.json 校验。失败抛 InvalidContract，附带字段路径。"""
    errors = sorted(_validator().iter_errors(dict(data)), key=lambda e: list(e.path))
    if not errors:
        return

    details = [
        {"path": "/".join(str(p) for p in err.path) or "<root>", "message": err.message}
        for err in errors
    ]
    raise InvalidContract(
        f"能力契约不合规，共 {len(errors)} 处问题",
        details={"violations": details},
    )


def load_contract(path: str | Path) -> CapabilityContract:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_contract(data)
    return CapabilityContract(data)


def load_contracts(paths: Iterable[str | Path]) -> list[CapabilityContract]:
    return [load_contract(p) for p in paths]


def validate_input(contract: CapabilityContract, params: Mapping[str, Any]) -> None:
    """按契约的 input_schema 校验入参。

    注意：这里**不做**权限判断。入参合法不等于有权调用，两者是不同层的事，
    混在一起会让人以为"schema 过了就是安全的"。
    """
    try:
        jsonschema.Draft202012Validator(dict(contract.input_schema)).validate(dict(params))
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise ValidationError(
            f"入参不合法：{path} — {exc.message}",
            details={"field": path, "capability_id": contract.capability_id},
        ) from exc
