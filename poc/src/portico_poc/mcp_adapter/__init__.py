"""把 Portico 能力以 remote MCP Server 形式挂到 Obot 上。

Obot 三种 runtime 里，只有 remote 与 containerized 会注入调用人归属头
（源码 apiclient/types/obotheaders.go 的 SupportsObotHeaders）。
本适配器因此固定以 remote 形式部署。
"""

from .attribution import (
    CallerAttribution,
    GroupPolicy,
    IdentityBridge,
    describe_expected_headers,
)
from .server import PorticoMcpApp, build_mcp, create_app_from_env

__all__ = [
    "CallerAttribution",
    "GroupPolicy",
    "IdentityBridge",
    "describe_expected_headers",
    "PorticoMcpApp",
    "build_mcp",
    "create_app_from_env",
]
