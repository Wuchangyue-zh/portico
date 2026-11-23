# Portico PoC —— 鉴权链骨架 + Obot 适配器

两件事：
1. 用 mock 把**两层鉴权**跑通并留下可复现证据
2. 提供一个**能被 Obot 以 remote MCP Server 加载**的适配器

```bash
cd poc
python -m pytest          # 71 项断言
python demo.py            # 人可读的鉴权链证据

# 启动适配器并用真实 MCP 协议验证
PORTICO_GATEWAY_SECRET=demo-secret PORTICO_PORT=8099 PYTHONPATH=src \
  python -m portico_poc.mcp_adapter.server &
PYTHONPATH=src python smoke_mcp.py
```

依赖：Python 3.11+、`jsonschema`、`mcp`、`pytest`。鉴权链本身无外部服务依赖。

挂到 Obot 上的完整步骤见 **[DEPLOY-OBOT.md](DEPLOY-OBOT.md)**。

---

## 这个 PoC 回答了什么问题

| 编号 | 问题 | 结论 |
|---|---|---|
| P01 | 两名员工调同一能力，能否只看到各自数据？ | ✅ mock 内成立，重叠 0 行；MCP 协议级也验证过 |
| P02 | 多角色叠加会不会意外放宽权限？ | ✅ 权限为显式并集，缺 `additional_grants` 的写能力不可见 |
| P04 | 调用人身份能否被伪造？ | ✅ 篡改/换密钥/过期令牌均被拒；无网关密钥的归属头被拒 |
| P06 | 绕过前端直调网关能否越权？ | ✅ 被拒，且与"能力不存在"返回同一文案 |
| P05 | 写入的幂等与乐观锁是否可靠？ | ✅ 同键重放零副作用；版本冲突独立报错 |

## 这个 PoC **没有**回答什么问题

写在最前面，因为这类"骨架跑通了"的成果最容易被误读成"底座选型验证完了"。

| 项 | 状态 |
|---|---|
| Obot 服务端完整启动 | ❌ 卡在 `failed to initialize Docker backend`；本机无 Docker，WSL sudo 需密码 |
| Obot 真实注入归属头 | ❌ 未测，需接上真实 Obot |
| 真实 OAuth / OBO 令牌交换 | ❌ 未测，mock 用 HMAC 模拟签名性质，不是 OAuth |
| 网络层防绕过（mTLS / NetworkPolicy） | ❌ 未测，只做了「必须有网关密钥头」的应用层强制 |
| 并发、超时、限流下的行为 | ⚠️ 超时用线程池模拟，线程无法真正终止，属已知局限 |
| 性能与容量指标 | ❌ 未测，一个数字都没有 |
| 真实 ERP / CRM 的业务规则与数据模型 | ❌ 未测，mock 数据是编的 |

> **结论定位**：证明的是**我们的设计能表达和强制什么**，不是**Obot 能满足什么**。
> 后者要等真实 Obot 跑起来后补测。参见 [DEPLOY-OBOT.md](DEPLOY-OBOT.md) 第 3 节。

---

## 关于 Obot 的实测发现（修正了 V1.1 文档）

读源码 + 本机启动验证得到三条，与 `docs/` 里的 V1.1 描述不一致：

| 发现 | 依据 |
|---|---|
| 默认数据库是 **SQLite**，不是 PostgreSQL | `--dsn` 默认值 `sqlite://file:obot.db?...`，实测建库与迁移成功 |
| 服务端**硬依赖**容器运行时，无「不启用」选项 | `MCPRuntimeBackend` 仅 docker/kubernetes/k8s 三值 |
| **remote MCP Server 能拿到调用人归属头** | `SupportsObotHeaders()` 对 `RuntimeRemote`/`RuntimeContainerized` 返回 true |

第三条最重要：它把「Obot 传不了员工身份」这个风险，收窄成了「Obot 的 **OpenAPI hosting runtime** 传不了」。
结论是**不需要分叉 Obot 源码**——把适配器做成 remote MCP Server 即可。

支持的归属头：`X-Obot-User-Id`、`X-Obot-User-Name`、`X-Obot-User-Email`、`X-Obot-IDP-User-Id`、`X-Obot-IDP-Groups`、`X-Obot-Api-Key-Id/Name`。

**但这些头没有签名**（源码里没有 HMAC），所以适配器必须叠加网关共享密钥 + 网络隔离。

---

## 设计里三个值得单独说明的决定

### 1. 令牌里不放权限

真实 OIDC 常把权限塞进 token claims，签发后到过期前无法收回。
BR-004 要求撤销立即生效，所以这里 token 只有 `sub/org/iat/exp`，
权限每次从目录重新解析。代价是每次调用多一次目录查询（可缓存 + 事件失效）。

对应测试：`test_token_carries_no_permissions`、`test_revocation_takes_effect_immediately`

### 2. 发现即鉴权

能力目录不返回"存在但你没权限"的条目。元数据本身就是信息——
能力名、字段名、枚举值足以还原业务结构。

对应测试：`test_discovery_does_not_leak_unauthorized_capabilities`

### 3. 越权显式请求要报错，不静默缩减

用户查"华北"但只有华南权限时，返回 FORBIDDEN 而不是"自动改成查华南"。
静默缩减会让用户以为"华北没数据"，然后拿这个假结论去做业务判断。

对应测试：`test_explicit_out_of_scope_region_is_rejected_not_silently_shrunk`

---

## 结构

```
poc/
├── src/portico_poc/
│   ├── identity.py        # 目录 + 令牌签发/校验（权限不进令牌）
│   ├── contract.py        # 契约加载、schema 校验、入参校验
│   ├── registry.py        # 能力注册与"发现即鉴权"
│   ├── policy.py          # 工具级授权 + 数据范围收紧（第一层）
│   ├── gateway.py         # GatewayPort 实现、超时、审计
│   ├── mcp_adapter/       # 挂到 Obot 上的 remote MCP Server
│   │   ├── attribution.py # X-Obot-* 归属头 → Actor（信任入口）
│   │   └── server.py      # FastMCP 工具定义与调用收口
│   └── adapters/
│       ├── base.py        # ExecutionContext / Adapter 协议
│       ├── mock_erp.py    # 服务账号型遗留系统（适配器承担业务鉴权）
│       └── mock_crm.py    # 支持用户级权限的系统（源系统承担鉴权）
├── tests/                 # 71 项断言，按 P01/P02/P04/P06/归属头 分组
├── demo.py                # 人可读证据输出
├── smoke_mcp.py           # 真实 MCP 协议级冒烟
└── DEPLOY-OBOT.md         # 挂到 Obot 的操作手册
```

两个 mock 适配器刻意做成相反的极端，用来验证同一个契约能同时表达两种情况：

| | mock_erp | mock_crm |
|---|---|---|
| `actor_propagation` | service_account_with_actor_validation | oauth_delegated |
| `data_scope.enforced_by` | adapter | source_system |
| 谁负责业务鉴权 | 适配器（需更严评审） | 源系统 |

---

## GatewayPort 的替换边界

`gateway.py` 里的 `GatewayPort` 协议只有 `discover` / `invoke` 两个方法。
真实接入 Obot 时，实现换掉、接口不动，Master Agent 与适配器不用改。

这不是过度设计——ADR-002 的退出条件明确写了"身份链无法可信闭环时要能换底座"，
如果业务代码直接调 Obot 私有 API，这个退出条件就没法执行。

---

## 下一步

1. **装上 Docker**，把 Obot 真正跑起来（本机卡在这一步）
2. 按 [DEPLOY-OBOT.md](DEPLOY-OBOT.md) 注册适配器，补测 P01/P02/P04/P06 的真实归属头传递
3. 把 `mock_erp.py` 换成真实 ERP 的 REST 调用——契约与鉴权链不用改
4. 补 P08（30–100 工具的能力发现准确率）——目前只有 2 个能力，样本不足
