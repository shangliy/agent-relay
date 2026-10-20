# Agent Relay

**中文** · [English](#english)

> 网页发布任务，手机接单，电脑执行。非盈利的 Agent 任务互助平台（邀请制试点）。

发布者把任务、模型和积分预算固定下来；接单者用手机一键接单，由**自己电脑上**的 Agent（Claude Code / Codex / 兼容 OpenAI 的 API）执行，按实际 token 用量获得积分。积分**不能提现**，只用于请求别人的帮助；累计贡献前三可以在榜单展示自己的作品。

## 怎么用

### 发布任务（网页或手机）

1. 用 GitHub 登录（首次需要邀请码）。新账户有 **20 RP 信用额度**，不用先赚积分。
2. 点「发布」：选任务类型 → 写标题、贴材料 → 从**当前在线的模型**里选一个 → 拖动预算上限。
3. 发布后在任务页实时看进度和结果。按实际用量结算，不超过上限，剩余退回。

六类模板：Blog 文章、研究简报、论文想法、论文评审（仅公开论文）、产品优化方案、视频策划。

### 接单赚积分（手机 + 电脑）

1. **连接电脑（一次）**：网页「我的 → 设备 → 生成连接命令」，复制到电脑终端运行：
   ```bash
   pip install "git+https://github.com/shangliy/agent-relay"
   agent-relay-worker connect XXXX-XXXX --server https://<平台地址>
   ```
   网页会自动显示「已连接」。之后保持这个终端窗口打开即可；下次直接运行 `agent-relay-worker run`。
2. **启用模型**：只启用你**有权用于他人任务**的资源。运行 `agent-relay-worker doctor` 查看状态：

   | 执行端 | 启用方式 |
   |---|---|
   | Claude Code | `export ANTHROPIC_API_KEY=...`（只走 API key，不会使用订阅登录额度） |
   | Codex | `export RELAY_CODEX_MODELS=<模型,...>` |
   | OpenAI 兼容 API | `export RELAY_HTTP_BASE_URL=... RELAY_HTTP_API_KEY=... RELAY_HTTP_MODELS=<模型,...>` |

3. **手机接单**：打开「接单」，默认只显示你的电脑能做的任务，点「接单」即可。平台自动派给合适的电脑，电脑执行、检查、回传，积分到账。

> 小技巧：在手机浏览器里「添加到主屏幕」，用起来就像一个 App。

### 须知

- 任务的输入和结果都是**公开**的。不要提交私人凭据、保密审稿材料或未获授权的数据。
- 执行者能看到任务明文，也能修改自己的客户端；平台无法证明模型真的被运行，**这不是保密计算**。
- 失败但用量已知的任务照常结算；用量未知按 0 结算。「格式检查通过」不代表内容正确。
- Alpha 试点：可能有故障、规则调整或数据重置。

## 部署与开发

```bash
pip install -e '.[dev]'
python -m pytest -q

# 本地运行（开发登录 + echo 测试执行端，生产环境默认关闭）
RELAY_DEV_LOGIN=1 RELAY_TEST_ADAPTERS=1 RELAY_ADMIN_TOKEN=dev python -m relay --port 8787
# 另一个终端：
agent-relay-worker --enable-test-adapters connect <配对码> --server http://127.0.0.1:8787
```

- 部署（Fly.io + GitHub OAuth）与运维：[`docs/PILOT_RUNBOOK.md`](docs/PILOT_RUNBOOK.md)
- 设计文档：[`docs/design_plan_cn.md`](docs/design_plan_cn.md)

```
relay/                 控制面（FastAPI + SQLite）：任务、租约、结算、榜单、登录、限流
relay/static/          手机优先的网页（PWA）
agent_relay_worker/    本地客户端（只依赖标准库）：配对、执行、回执落盘重传
tests/                 结算、租约、并发、社区、试点功能、端到端
```

### 状态

- **已实现并有测试**：积分预留与只结算一次、运行租约与 fencing token、失联恢复、一键接单自动选设备、在线模型列表、GitHub 登录与邀请码、限流与安全头、举报与暂停推荐位、本地客户端单实例与回执重传。
- **待验证**：Claude Code / Codex / HTTP 执行端还没有用真实账号跑过完整付费调用。
- **规划**：附件与文件交付、反互刷、主 Agent 拆分子任务、视频成片、签名安装包。

许可证：MIT

---

<a id="english"></a>

# Agent Relay (English)

[中文](#agent-relay) · **English**

> Publish tasks on the web, claim them from your phone, run them on your computer. A non-profit agent task exchange (invite-only pilot).

Publishers fix a task, its model and a credit budget. Contributors claim tasks from their phone with one tap; the work runs on **their own computer** through a local agent (Claude Code / Codex / any OpenAI-compatible API) and earns credits based on reported token usage. Credits **cannot be cashed out**; they only buy help from others. The top three contributors can showcase their own projects on the leaderboard.

## How to use

### Publish a task (web or phone)

1. Sign in with GitHub (an invite code is needed the first time). New accounts get a **20 RP credit line**, so you can publish right away.
2. Tap **Publish**: pick a task type, write a title, paste your material, choose one of the **models currently online**, and set a budget cap.
3. Follow progress and read the result on the task page. You pay actual usage, never more than the cap; the rest is released.

Templates: blog post, research brief, paper ideas, paper review (public papers only), product improvement plan, video plan.

### Earn credits (phone + computer)

1. **Connect a computer (once)**: on the web, go to *Me → Devices → Generate command*, then paste it into a terminal:
   ```bash
   pip install "git+https://github.com/shangliy/agent-relay"
   agent-relay-worker connect XXXX-XXXX --server https://<relay-url>
   ```
   The page shows *Connected* automatically. Keep that terminal open; next time just run `agent-relay-worker run`.
2. **Enable a model**: only use resources you are **allowed to use for other people's tasks**. Check with `agent-relay-worker doctor`:

   | Engine | How to enable |
   |---|---|
   | Claude Code | `export ANTHROPIC_API_KEY=...` (API key only; subscription login quota is never used) |
   | Codex | `export RELAY_CODEX_MODELS=<model,...>` |
   | OpenAI-compatible API | `export RELAY_HTTP_BASE_URL=... RELAY_HTTP_API_KEY=... RELAY_HTTP_MODELS=<model,...>` |

3. **Claim from your phone**: open **Claim**. By default it lists only tasks your computer can run. Tap **Claim**; the platform assigns it to a matching computer, which runs, checks and uploads the result, and your credits arrive.

> Tip: use "Add to Home Screen" in your phone's browser to use it like an app.

### Please note

- Task inputs and results are **public**. Do not submit credentials, confidential review material or data you are not authorized to share.
- The contributor sees the task in plain text and controls their own client; the platform cannot prove a model actually ran. **This is not confidential computing.**
- Failed runs with known usage are still paid; unknown usage settles as zero. "Format check passed" does not mean the content is correct.
- Alpha pilot: expect bugs, rule changes or data resets.

## Deploy & develop

```bash
pip install -e '.[dev]'
python -m pytest -q

# Local run (dev login + model-free echo engine; both off in production)
RELAY_DEV_LOGIN=1 RELAY_TEST_ADAPTERS=1 RELAY_ADMIN_TOKEN=dev python -m relay --port 8787
# In another terminal:
agent-relay-worker --enable-test-adapters connect <pair-code> --server http://127.0.0.1:8787
```

- Deployment (Fly.io + GitHub OAuth) and operations: [`docs/PILOT_RUNBOOK.md`](docs/PILOT_RUNBOOK.md) (Chinese)
- Design document: [`docs/design_plan_cn.md`](docs/design_plan_cn.md) (Chinese)

### Status

- **Implemented and tested**: credit reservation with once-only settlement, run leases with fencing tokens, stalled-run recovery, one-tap claim with automatic device selection, live model supply, GitHub login with invites, rate limits and security headers, reports and showcase suspension, single-instance worker with durable receipts.
- **Pending verification**: the Claude Code / Codex / HTTP engines have not yet completed a paid run on real accounts.
- **Planned**: attachments and file deliverables, anti-collusion rules, orchestrator sub-tasks, video rendering, signed installers.

License: MIT
