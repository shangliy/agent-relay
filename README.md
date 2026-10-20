# Agent Relay

非盈利的 Agent 任务互助服务：发布者固定任务与预算，参与者在网页/手机接单，由自己电脑上的本地 worker 调用本地 Agent 执行，按已报告的 token 用量结算不可提现积分。

设计依据：[`docs/design_plan_cn.md`](docs/design_plan_cn.md)。

本仓库是从零开始的本地优先实现（Python FastAPI + SQLite），直接按设计文档的 **v2 协议**（任务/运行分离、租约 + fencing token、运行级幂等结算）实现，不继承第 3 版原型的 v1 API。

## 参与试点（接单者）

试点为邀请制：用 GitHub 登录，首次需要邀请码。

```bash
pip install "git+https://github.com/shangliy/agent-relay"
agent-relay-worker doctor        # 查看本机可用的执行端
# 网页「设备」页生成配对码后：
agent-relay-worker pair XXXX-XXXX --server https://<试点地址>
agent-relay-worker run
```

只启用你有权用于第三方任务的资源（见下表）。任务输入与结果都是公开的；执行者能看到任务明文，这不是保密计算。

## 本地开发

```bash
pip install -e '.[dev]'
python -m pytest -q                              # 46 tests

# 控制面（开发登录 + 允许 echo 测试适配器；两者在生产默认关闭）
RELAY_DEV_LOGIN=1 RELAY_TEST_ADAPTERS=1 RELAY_ADMIN_TOKEN=dev python -m relay --port 8787
# 打开 http://127.0.0.1:8787 → 登录 → 「设备」生成配对码

# 本地 worker（只依赖标准库）
python -m agent_relay_worker doctor
python -m agent_relay_worker pair ABCD-1234 --server http://127.0.0.1:8787
python -m agent_relay_worker run                 # 加 --enable-test-adapters 可用 echo
```

部署与运维见 [`docs/PILOT_RUNBOOK.md`](docs/PILOT_RUNBOOK.md)。

真实执行端开关（只启用你确认有权用于第三方任务的资源）：

| 执行端 | 启用条件 |
|---|---|
| `claude_code` | 设置 `ANTHROPIC_API_KEY`（`--bare` 只走 API key，不用订阅额度）；`RELAY_CLAUDE_MODELS` 可覆盖模型列表 |
| `codex` | 设置 `RELAY_CODEX_MODELS=<逗号分隔>` |
| `http` | `RELAY_HTTP_BASE_URL`、`RELAY_HTTP_API_KEY`、`RELAY_HTTP_MODELS` |

## 结构

```
relay/                 控制面
  api.py               /v2 HTTP 接口（§12）
  service.py           业务逻辑：配对、发布/预留、接单/租约、结算、榜单、对账
  db.py                SQLite schema（追加式迁移）
  spec.py              任务包 v2：规范化 JSON + sha256
  oauth.py             GitHub OAuth
  web.py               限流、请求体上限、安全头
  validators.py        固定版本的确定性检查（nonempty/json/json_schema/min_words）
  templates.py         六类任务模板
  static/              手机优先的网页大厅（index.html + app.js，满足 CSP）
agent_relay_worker/    本地执行面（纯标准库）
  worker.py            单实例锁、轮询、start→heartbeat→finish、回执落盘重传
  adapters.py          echo / claude_code / codex / http
tests/                 结算、租约、并发、社区、worker 端到端
```

## 状态（沿用设计文档的标记）

**已实现（有自动化测试）**
- 积分规则：1 RP = 1000 milli；费用 = min(预算, 输入×1 + 输出×3)；初始 0、信用 20 RP、单任务 1–100 RP；负余额禁止发布
- 发布时预留，结算时整额释放 + 实付转移；双边分录；`/v2/admin/reconcile` 检查账本不变量
- 运行租约：原子抢单、递增 fencing token、单任务/单设备至多一个活跃 run（部分唯一索引）、必须 `start` 成功才能调用模型、心跳续租有硬上限
- 租约过期 → 任务 `stalled`，不自动重跑；发布者需显式确认重复成本后 `reopen`；晚到回执只存证不结算
- 结算幂等：同回执重复提交返回原结果，不同回执 409；创建/接单支持 `Idempotency-Key`
- 已知用量的失败任务照常结算；未知用量按 0 结算并标记 `unknown`
- 停止请求经心跳下发；已发生用量照常结算
- worker：单实例文件锁、回执先 fsync 落盘再上传、断网重传不重复调用模型、子进程环境不含平台密钥
- 榜单：累计正向收入前 10，前 3 且公开名片才展示作品（不顺延）；关闭公开立即生效；名片仅允许 https 链接
- 运营开关：`RELAY_ALLOW_NEW_TASKS` / `RELAY_ALLOW_NEW_CLAIMS` / `RELAY_SHOWCASE`
- 试点：GitHub OAuth 登录（无 scope）、邀请码、30 天会话 HttpOnly cookie、cookie 写请求校验来源、按 IP 限流、请求体上限、CSP 等安全头、举报与暂停推荐位、`/v2/admin/stats` 指标
- worker 拒绝向非 HTTPS 服务器发送设备密钥（本机除外）

**待验证**
- `claude_code`、`codex`、`http` 适配器：参数与输出解析按官方文档和 CLI `--help` 编写，解析器有样例测试，但**尚未用真实账号跑过付费调用**（P0 退出条件）
- 网页大厅：真实手机/桌面浏览器验收未做

**规划（未实现）**
- 附件与成果对象存储、artifact 上传接口
- 举报、推荐资格、反互刷；主 Agent 父子任务；受限工具；视频成片
- 多机部署（共享限流存储）、签名安装包与自动更新

## 下一步（P0 真实闭环）

1. 选定一个执行端（建议 `claude_code` + API key），用真实账号跑通一个跨账户任务，记录 CLI 版本、模型、真实 usage 和失败样本
2. 手机 + 桌面浏览器完整走一遍：配对 → 接单 → 执行 → 结果 → 积分
3. 两位用户完成跨账户任务，确认重复回执不重复付费
