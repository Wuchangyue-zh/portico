# 把 Portico 适配器挂到 Obot 上

本文是操作手册，基于**源码阅读**（Obot `1bdd468`，v0.28.2，2026-11-20）与**本机实测启动**。
标注为「未验证」的步骤，是因为本机没有 Docker，Obot 服务端启动在 Docker 后端初始化处中止。

> 证据等级：`[源码]` 已读源码确认 · `[实测]` 本机跑过 · `[未验证]` 需在有 Docker 的环境补测

---

## 0. 前置条件

| 项 | 说明 |
|---|---|
| 容器运行时 | **必需**。`MCPRuntimeBackend` 只有 `docker` / `kubernetes` / `k8s` 三个取值，没有"不启用"选项 `[源码]` |
| 数据库 | **可选**。默认 DSN 是 `sqlite://file:obot.db?...`，无需 PostgreSQL `[实测]` |
| 端口 | 默认 8080（HTTP），内部 apiserver 另起一个端口 `[实测]` |

启动命令（本机实测有效，除 Docker 外全部通过）：

```bash
./obot.exe server \
  --http-listen-port 8080 \
  --mcpruntime-backend docker \
  --disable-update-check
```

启动日志中应看到：

```
Connecting to database  dsn=sqlite://file:obot.db?...
Database migrations completed successfully
```

然后若出现 `failed to initialize Docker backend`，即缺 Docker，属本文档覆盖面之外。
（顺带一提：官方推荐的 `docker run` 命令带 `-v /var/run/docker.sock:/var/run/docker.sock`，
这意味着拥有创建 MCP Server 权限的 Obot 用户可以操作宿主机 Docker，生产环境不要这样部署。）

## 1. 启动 Portico 适配器

```bash
cd poc
PORTICO_GATEWAY_SECRET="$(openssl rand -hex 32)" \
PORTICO_PORT=8099 \
PYTHONPATH=src python -m portico_poc.mcp_adapter.server
```

记下这个 `PORTICO_GATEWAY_SECRET`，下一步要用。

适配器暴露 streamable HTTP 端点：`http://<host>:8099/mcp`。

## 2. 在 Obot 中注册为 remote MCP Server

关键三项，缺一不可：

### 2.1 runtime 必须是 `remote`

`[源码]` `apiclient/types/obotheaders.go`：

```go
func SupportsObotHeaders(runtime Runtime) bool {
    return runtime == RuntimeRemote || runtime == RuntimeContainerized
}
```

`runtime=openapi`（Obot 自带的 OpenAPI 自动包装）**拿不到**调用人归属头。
带身份与细粒度数据权限的能力，一律走 remote，这是本项目不用 Obot OpenAPI hosting 的代码级依据。

### 2.2 勾选 injectObotHeaders

至少要勾这两个：

| Header | 用途 |
|---|---|
| `X-Obot-User-Id` | 数据归属键（例如客户 owner 比对） |
| `X-Obot-IDP-Groups` | 权限与数据范围来源，CSV 编码 |

可选：`X-Obot-User-Name`、`X-Obot-User-Email`、`X-Obot-IDP-User-Id`。

`[源码]` 组的取值来自 `caller.GetExtra()["auth_provider_groups"]`——
即企业 IdP 登录时带过来的组声明，不是 Obot 自己维护的。所以组名必须和你 IdP 里的组名一致。

### 2.3 配置网关共享密钥（静态 header）

```
X-Portico-Gateway-Secret: <第 1 步生成的那个值>
```

**为什么必须有这个头**：`[源码]` `pkg/mcp/attribution` 包里只有 `Strip`（剥掉客户端自带的同名头）
和 `ForCaller`（从已验签的 caller 派生），**没有任何 HMAC 或签名**。
X-Obot-* 是明文 HTTP 头。谁能让请求打到你适配器的 8099 端口，谁就能伪造 `X-Obot-User-Id: 管理员`。

共享密钥只是纵深防御，**真正的边界是网络隔离**：

- 适配器监听在只有 Obot 可访问的网段 / NetworkPolicy
- 不要给 8099 做公网端口映射
- 有条件时上 mTLS

对应能力契约里的 `actor_propagation.bypass_protection` 字段——这个字段在 schema 里是强制的，
写不出答案的适配器不得发布。

## 3. 验证

`[实测]` 本机已通过的 MCP 协议级冒烟（不含 Obot 层）：

```bash
cd poc
PYTHONPATH=src python smoke_mcp.py
```

期望输出：

```
[1] 握手成功 server=portico protocol=2025-11-25
[2] 工具列表 ['crm_customer_update_note', 'erp_order_search', 'list_capabilities']
[3] 华南销售 → 区域=['south-china'] 行数=30
[4] 华北销售 → 区域=['north-china'] 行数=30
[5] 伪造身份（无网关密钥）→ UNAUTHORIZED
[6] 越区查询 → FORBIDDEN
```

接入 Obot 后要补的验证（`[未验证]`）：

| 项 | 怎么看 |
|---|---|
| P01 双员工隔离 | 用两个 IdP 账号登录同一个 Obot，调同一工具，看返回区域 |
| P04 归属头真实性 | 抓取适配器收到的请求头，确认 `X-Obot-User-Id` 与登录账号一致 |
| P06 绕过网关 | 找到适配器地址直接 POST，不带密钥头，应被拒 |
| P02 Profile 叠加 | 给同一用户勾多个 Profile，确认有效工具集没有意外放宽 |

## 4. 已知的坑

| 坑 | 说明 |
|---|---|
| `injectObotHeaders` 在 vMCP 层是**并集** `[源码]` | `pkg/mcp/vmcp.go` 用 `map[ObotHeader]struct{}` 聚合各组件的头。聚合后某些组件会收到本来没要求的头——适配器不能假设"收到了这个头就说明我真的需要它" |
| 组件网关会复用连接 JWT | `[源码]` 注释写明 connection 的 JWT 可能属于早先的请求，因此内部复合网关只转发"本次请求选中的值"，不做重新派生 |
| 未启用 auth 时归属头为空 | 评测模式（无认证）下没有 caller，`ForCaller` 返回空，适配器会 fail-closed 拒绝。这是预期行为 |
| 只配 `X-Obot-User-Id` 不够 | 权限与数据范围来自组。只配 user id 会导致 actor 无任何权限，能力全部不可见 |

## 5. 下一步

1. 装上 Docker（或用 WSL2 里的 Docker），把 Obot 真正跑起来
2. 按本手册注册适配器，补测上表四项
3. 把 `poc/src/portico_poc/adapters/mock_erp.py` 换成真实 ERP 的 REST 调用——契约与鉴权链不用改
