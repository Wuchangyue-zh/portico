"""冒烟：用真实 MCP 协议连接跑起来的适配器，验证它能被 MCP 客户端加载。

这个脚本是"能被 Obot 挂载"的最小证据：Obot 加载 remote MCP Server 走的是同一套
streamable HTTP + MCP 握手。区别只在 Obot 会把归属头注入进来。

用法（需要适配器已在 8099 运行）：
    PORTICO_GATEWAY_SECRET=demo-secret PORTICO_PORT=8099 \
        PYTHONPATH=src python -m portico_poc.mcp_adapter.server &
    PYTHONPATH=src python smoke_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("PORTICO_MCP_URL", "http://127.0.0.1:8099/mcp")
SECRET = os.environ.get("PORTICO_GATEWAY_SECRET", "demo-secret")


def attribution(user: str, groups: str) -> dict[str, str]:
    return {
        "x-portico-gateway-secret": SECRET,
        "x-obot-user-id": user,
        "x-obot-user-name": user,
        "x-obot-idp-groups": groups,
    }


async def main() -> int:
    failures: list[str] = []

    async def session_for(headers: dict[str, str]):
        return streamablehttp_client(URL, headers=headers)

    # ---- 1. 握手 + 工具列表 ----
    async with await session_for(attribution("U-1001", "portico-sales-south")) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print(f"[1] 握手成功 server={init.serverInfo.name} protocol={init.protocolVersion}")
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"[2] 工具列表 {names}")
            if "erp_order_search" not in names:
                failures.append("未暴露 erp_order_search")

    # ---- 2. 华南销售查单 ----
    async with await session_for(attribution("U-1001", "portico-sales-south")) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "erp_order_search",
                {"start_date": "2026-11-01", "end_date": "2026-11-22"},
            )
            payload = json.loads(res.content[0].text)
            regions = sorted({row["region"] for row in payload["result"]["rows"]})
            print(f"[3] 华南销售 → 区域={regions} 行数={len(payload['result']['rows'])}")
            if regions != ["south-china"]:
                failures.append(f"华南销售看到了非授权区域：{regions}")

    # ---- 3. 华北销售查单 ----
    async with await session_for(attribution("U-1002", "portico-sales-north")) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "erp_order_search",
                {"start_date": "2026-11-01", "end_date": "2026-11-22"},
            )
            payload = json.loads(res.content[0].text)
            regions = sorted({row["region"] for row in payload["result"]["rows"]})
            print(f"[4] 华北销售 → 区域={regions} 行数={len(payload['result']['rows'])}")
            if regions != ["north-china"]:
                failures.append(f"华北销售看到了非授权区域：{regions}")

    # ---- 4. 伪造身份：没有网关密钥 ----
    try:
        async with await session_for(
            {"x-obot-user-id": "U-1001", "x-obot-idp-groups": "portico-sales-north"}
        ) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(
                    "erp_order_search", {"start_date": "2026-11-01", "end_date": "2026-11-22"}
                )
                payload = json.loads(res.content[0].text)
                code = payload.get("error", {}).get("code")
                print(f"[5] 伪造身份（无网关密钥）→ {code}")
                if code != "UNAUTHORIZED":
                    failures.append(f"伪造身份未被拒绝，返回 {code}")
    except Exception as exc:  # noqa: BLE001 - 连接层拒绝也算通过
        print(f"[5] 伪造身份 → 连接层拒绝（{type(exc).__name__}）")

    # ---- 5. 越区查询 ----
    async with await session_for(attribution("U-1001", "portico-sales-south")) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "erp_order_search",
                {"start_date": "2026-11-01", "end_date": "2026-11-22", "region": "north-china"},
            )
            payload = json.loads(res.content[0].text)
            code = payload.get("error", {}).get("code")
            print(f"[6] 越区查询 → {code}")
            if code != "FORBIDDEN":
                failures.append(f"越区查询未被拒绝，返回 {code}")

    print()
    if failures:
        print("失败：")
        for f in failures:
            print("  -", f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
