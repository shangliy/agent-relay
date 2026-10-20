# 试点运维手册（Fly.io + GitHub OAuth，邀请制）

## 一次性部署

```bash
# 0. 安装并登录 flyctl
curl -L https://fly.io/install.sh | sh
fly auth login

# 1. 创建应用和持久卷（名字全局唯一；若被占用，同时改 fly.toml 的 app 和 RELAY_PUBLIC_URL）
fly apps create agent-relay-pilot
fly volumes create relay_data --region sjc --size 1 -a agent-relay-pilot

# 2. GitHub → Settings → Developer settings → OAuth Apps → New
#    Homepage URL:               https://agent-relay-pilot.fly.dev
#    Authorization callback URL: https://agent-relay-pilot.fly.dev/v2/auth/github/callback
#    不需要任何 scope；平台只保存 GitHub 数字 id 和用户名。

# 3. 机密只放在 Fly secrets，不进仓库
fly secrets set -a agent-relay-pilot \
  GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... \
  RELAY_ADMIN_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"

# 4. 部署
fly deploy
curl https://agent-relay-pilot.fly.dev/healthz
```

保存好 `RELAY_ADMIN_TOKEN`，下面记为 `$ADMIN`，`$URL=https://agent-relay-pilot.fly.dev`。

## 日常操作

```bash
H="Authorization: Bearer $ADMIN"

# 发邀请码（每个码默认 1 次、30 天有效）
curl -s -XPOST $URL/v2/admin/invites -H "$H" -H 'Content-Type: application/json' \
  -d '{"n":10,"max_uses":1,"note":"pilot wave 1","ttl_days":30}'

# 试点指标（技术完成率分母含所有已启动运行；未知用量比例）
curl -s $URL/v2/admin/stats -H "$H"

# 账本对账：ok 必须为 true
curl -s $URL/v2/admin/reconcile -H "$H"

# 举报处理：暂停推荐位（不扣积分、不改排名）
curl -s $URL/v2/admin/reports -H "$H"
curl -s -XPOST $URL/v2/admin/accounts/<account_id>/showcase -H "$H" -H 'Content-Type: application/json' \
  -d '{"suspended":true,"report_id":1,"note":"spam link"}'
```

## 三个独立开关（第 14 节）

出问题时只关需要关的部分，历史页面保持可读：

```bash
fly secrets set RELAY_ALLOW_NEW_TASKS=0   # 停止新发布
fly secrets set RELAY_ALLOW_NEW_CLAIMS=0  # 停止新接单
fly secrets set RELAY_SHOWCASE=0          # 关闭推荐位
```

（`fly secrets set` 会触发重启，约数秒。）

## 停止条件（第 13 节）

出现以下任一情况：**立即关闭新发布和新接单**，保留数据库快照，先恢复一致性再继续。

- `reconcile` 返回 `ok: false`（重复付费、预留不符、账本不守恒）
- 越权读取、凭据泄露、失控的外部调用

## 备份与恢复

- Fly 会给卷做每日快照：`fly volumes snapshots list <vol_id>`
- 手动备份：`fly ssh console -C "python -c \"import sqlite3; s=sqlite3.connect('/data/relay.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)\""`，再 `fly ssh sftp get /data/backup.db`
- 迁移只追加（`relay/db.py` 的 `MIGRATIONS`），启动时自动执行；升级前先备份

## 已知限制（对测试者如实说明）

- 单机部署：限流在内存里，SQLite 只有一个卷。不要把 machine 扩到多台。
- 真实执行端（claude_code / codex / http）的完整付费链路还需要首批测试者验证；`unknown` 用量按 0 结算。
- 没有附件和文件上传，只交付文本。
- 本地客户端不是签名安装包；执行隔离只靠临时目录和禁用工具，不是虚拟机。
