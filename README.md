# memorys — 跨会话 AI 知识库

给人和 agent 共用的记忆库。换模型、换 agent、换会话之后，从这里把项目上下文一次性读回来。

- 存储：md 文件是 source of truth（带 YAML frontmatter），PostgreSQL 只做索引
- 隔离：按用户物理隔离目录 `data/users/u{id}/{library}/*.md`，服务端强制校验
- 版本：每个用户目录是独立 git repo，写入即 commit，可回滚、可 push 到 GitHub
- 三层接入：REST API / MCP Server / Web UI，同一套鉴权

线上地址：https://utils.xlingo.fun/mem/

## 架构

```
浏览器 / agent / 脚本
        │
   nginx (utils.xlingo.fun/mem/)
        │
   127.0.0.1:8649  systemd: memorys.service
        │
   ┌────┴─────────────────────────┐
   │ FastAPI                      │
   │  /api/v1/*   REST            │
   │  /mcp        MCP streamable  │
   │  /           Web UI          │
   └────┬─────────────────────────┘
        │
   ┌────┴────┐         ┌──────────────────────────┐
   │ PG 18   │ 索引    │ /var/lib/memorys/data/   │ md + git
   │ :54321  │         │  users/u{id}/{lib}/*.md  │
   └─────────┘         └──────────────────────────┘
```

## 认证

复用 ai.xlingo.fun（xiaoai-chat）的账号体系，不另建一套用户密码。

三种进入方式：

| 方式 | 头 | 说明 |
|---|---|---|
| 账号密码 | — | `POST /api/v1/auth/ai-login {username, password}`，后端转发到 ai.xlingo.fun 校验（真实密码 + 2FA 都由上游处理），成功后同步用户并签发本地 JWT（12h） |
| 上游 token | `Authorization: Bearer <xiaoai token>` | 与 xiaoai-chat 共享 HS256 secret，本地直接验签。本地签发的 token 带 `iss=memorys`，据此区分两条路径 |
| API Key | `X-Api-Key: hk_xxx` | 给 agent / 脚本 / MCP 客户端用，Web UI 里创建，sha256 存哈希，可吊销 |

## REST API

全部需要认证。响应为直出 JSON（非信封）。

```
POST   /api/v1/auth/ai-login          登录（username+password 或 accessToken）
GET    /api/v1/auth/me                当前用户

GET    /api/v1/documents              列表（?library= &project= &type= &trash=true &limit=）
POST   /api/v1/documents              新建（同库同标题返回 409 + 已存在的 documentId）
GET    /api/v1/documents/{id}         全文
PATCH  /api/v1/documents/{id}         更新（可带 expectedHash 做乐观锁）
DELETE /api/v1/documents/{id}         软删除（进回收站 + .trash 目录）
POST   /api/v1/documents/{id}/restore 恢复
GET    /api/v1/documents/{id}/history git 提交历史

GET    /api/v1/search                 检索（?q= &limit= &library= &project=）
GET    /api/v1/bootstrap              上下文引导包（?project= &token_budget=4000）

GET    /api/v1/keys                   API Key 列表
POST   /api/v1/keys                   创建（返回的 key 只出现这一次）
DELETE /api/v1/keys/{id}              吊销

POST   /api/v1/sync                   从磁盘重建索引（?pull=true 先 git pull）
POST   /api/v1/sync/push              commit + push 到 GitHub
GET    /api/health                    健康检查（免认证）
```

例：

```bash
K=hk_xxx
B=https://utils.xlingo.fun/mem

curl -H "X-Api-Key: $K" "$B/api/v1/search?q=xspeak%20怎么发版"
curl -H "X-Api-Key: $K" "$B/api/v1/bootstrap?project=xspeak&token_budget=4000"
curl -H "X-Api-Key: $K" -H 'Content-Type: application/json' \
  -d '{"title":"标题","type":"fact","content":"正文","project":"xspeak"}' \
  "$B/api/v1/documents"
```

## MCP

streamable HTTP，端点 `https://utils.xlingo.fun/mem/mcp`，stateless（多 agent 并发挂载不需要维持会话）。

10 个工具：

| 工具 | 用途 |
|---|---|
| `memory_search` | 检索记忆，返回 chunk + 所属文档 |
| `memory_get` | 按 id 取全文 |
| `memory_write` | 写入（同标题会返回已存在提示，不静默重复写） |
| `memory_update` | 更新 |
| `memory_delete` | 软删除 |
| `memory_restore` | 从回收站恢复 |
| `memory_bootstrap` | 一次性拿项目上下文包，按 token 预算截断 |
| `memory_list_libraries` | 列库 |
| `memory_list_docs` | 列文档 |
| `memory_history` | 看 git 变更历史 |

客户端配置（以 Claude Code / Hermes 这类支持 HTTP MCP 的为例）：

```json
{
  "mcpServers": {
    "memorys": {
      "type": "http",
      "url": "https://utils.xlingo.fun/mem/mcp",
      "headers": { "X-Api-Key": "hk_xxx" }
    }
  }
}
```

新会话开场推荐用法：先 `memory_bootstrap(project="xspeak")` 把上下文拉回来，再按需 `memory_search`。

## 文档格式

```markdown
---
id: mem_01h9x...
title: xspeak 项目架构总结
type: project_summary     # project_summary | decision | preference | howto | glossary | fact
project: xspeak
tags: [nextjs, deploy]
importance: 5             # 1-5，影响 bootstrap 排序
source: agent:pi          # 谁写的
created_at: 2026-08-31T02:01:29Z
updated_at: 2026-08-31T02:01:29Z
---

# 正文 Markdown
```

frontmatter 里的额外字段会原样存进 `meta`，加 `expires_at` / `related_ids` 这类扩展不需要改 schema。

## 检索

中文场景默认不依赖 embedding：

1. jieba 分词 → PG `tsvector` + `ts_rank`（BM25 类）
2. `pg_trgm` 相似度兜底（错别字、部分词）
3. 两路结果 RRF 融合（k=60）
4. **每篇文档最多占 2 条**——否则一篇长文档的多个 chunk 会吃满整个结果集，agent 拿去恢复上下文等于白烧 token

配了 embedding 端点（`MEM_EMBED_API_BASE` + `MEM_EMBED_API_KEY`）就自动启用向量检索，走 pgvector 余弦距离。没配就是纯关键词，零依赖零成本。

md 按标题层级切 chunk（默认 1600 字），命中返回 chunk 而非全文，避免一次打爆 context。

## 部署

```
/opt/memorys/                  源码（git 仓库）
  app/                         后端 + Web UI
  tests/                       e2e / mcp / 公网链路测试
  deploy/                      systemd unit + nginx 片段
  .env                         配置（不入 git）
/var/lib/memorys/data/users/   数据（每用户一个 git repo）
```

```bash
systemctl status memorys
journalctl -u memorys -f
```

nginx 配置在 `/www/server/panel/vhost/nginx/utils.xlingo.fun.conf`，是标记好的一整段（`===== memorys 知识库 =====`），整段删掉即可回退。备份：`utils.xlingo.fun.conf.bak-mem-*`。

两个坑记一下：

- `location` 必须用 `^~` 前缀匹配。这个 vhost 底部有宝塔生成的 `location ~ .*\.(js|css)?$` 正则块，正则优先级高于普通前缀 location，不加 `^~` 静态资源会被正则块截走。
- `/mem/mcp` 单独给了一条 `location =` 精确匹配。后端不知道自己被挂在子路径下，访问不带尾斜杠的 `/mem/mcp` 时它会 307 到 `/mcp/`，把 `/mem` 前缀丢掉。

前端 `index.html` 会自己侦测 `/mem` 前缀并给 API 请求加回来（`BASE` 常量），所以挂子路径和挂域名根都能用。

### 配置项

`.env`，前缀 `MEM_`：

| key | 默认 | 说明 |
|---|---|---|
| `MEM_DATABASE_URL` | `postgresql+asyncpg://REDACTED@127.0.0.1:54321/memorys` | |
| `MEM_DATA_DIR` | `/var/lib/memorys` | md + git 根目录 |
| `MEM_JWT_SECRET` | 同 xiaoai-chat | 改了就不能验上游 token 了 |
| `MEM_UPSTREAM_BASE` | `https://ai.xlingo.fun` | 登录校验上游 |
| `MEM_MCP_ALLOWED_HOSTS` | 含 `utils.xlingo.fun` | MCP 的 DNS rebinding 白名单，换域名要加进来，否则 421 |
| `MEM_GITHUB_REMOTE` | 空 | 填 `git@github.com:user/repo.git` 后可用 `/api/v1/sync/push` |
| `MEM_EMBED_API_BASE` / `MEM_EMBED_API_KEY` | 空 | 配了才启用向量混合检索 |

### GitHub 长期备份

数据目录已经是 git repo。要推到 GitHub：先在 GitHub 建好私有库，把地址填进 `MEM_GITHUB_REMOTE`，然后 Web UI「同步 / GitHub」点推送，或 `POST /api/v1/sync/push`。

本机 GitHub SSH 免密已配好（账号 xiaocqaq）。远端地址必须用 `git@github.com:...` 形式，写 `https://` 会报 could not read Username。

多用户各自一个 repo，`MEM_GITHUB_REMOTE` 是全局的，多用户场景需要改成 per-user 配置（当前单用户自用，没做）。

## 测试

```bash
cd /opt/memorys
PYTHONPATH=/opt/memorys .venv/bin/python tests/e2e_test.py         # REST 27 项
PYTHONPATH=/opt/memorys .venv/bin/python tests/mcp_test.py         # MCP 14 项
MEM_TEST_KEY=hk_xxx .venv/bin/python tests/public_mcp_test.py      # 公网链路 6 项
```

e2e 自带清理（只清 memtest / memtest2 两个测试用户），可重复跑。

## 待办

- `mem.xlingo.fun` 独立域名：DNS 在百度云（NS `ns1/ns2.bdydns.cn`），需要加一条 A 记录指 `115.159.206.76`，再签证书。当前走 `utils.xlingo.fun/mem/`。
- 向量检索代码已就位但未实测（缺 embedding 端点）。
- `MEM_GITHUB_REMOTE` 目前是全局单值，多用户需改 per-user。
