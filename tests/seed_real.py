"""往真实账号 xiao 的库里写入第一批记忆（memorys 项目自身的交接信息）。

用 API Key 走公网 HTTPS，等于真实 agent 的调用路径。
"""
import json
import os
import sys

import httpx

BASE = "https://repo.xlingo.fun"
KEY = open("/root/.memorys-hermes-key").read().strip()
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}

DOCS = [
    {
        "title": "memorys 项目架构总结",
        "type": "project_summary",
        "project": "memorys",
        "tags": ["fastapi", "postgres", "mcp", "knowledge-base"],
        "importance": 5,
        "source": "agent:hermes",
        "content": """# 是什么

memorys 是 AI 跨会话知识库：换模型、换 agent、换会话之后，依旧能从这里读回项目记忆，快速恢复工作上下文。
站点 https://repo.xlingo.fun ，代码 /opt/memorys ，数据 /var/lib/memorys/data 。

## 核心设计

Markdown 是 source of truth，PostgreSQL 只做检索索引。DB 丢了可以用 `/api/v1/sync` 从磁盘 md 全量重建。
每个用户一个独立 git repo（`/var/lib/memorys/data/users/u<id>/`），写入即 commit，可推 GitHub 长期备份。

## 三层接入

- REST API：`/api/v1/documents`、`/api/v1/search`、`/api/v1/bootstrap`，交互文档 `/api/docs`
- MCP server：streamable HTTP 挂在 `/mcp/`，10 个工具给 agent 直接调用
- Web UI：单文件 `app/static/index.html`，暗色主题，登录/检索/编辑/回收站/Key 管理

## 认证

复用 ai.xlingo.fun（xiaoai-chat）账号：`POST /api/v1/auth/ai-login` 拿用户名密码去上游验，
成功后按 `identity_users.id` upsert 本地用户并签发 memorys 自己的 JWT。密码校验/2FA/封禁全在上游，本地不存密码。
agent 用 API Key（`hk_` 前缀）认证，与 JWT 等价。

## 技术栈

Python 3.11 + FastAPI + SQLAlchemy(asyncpg) + PostgreSQL 18（宝塔原生，127.0.0.1:54321）+ jieba + mcp 1.29.1。
不用 Docker，systemd 直接跑 venv 里的 uvicorn，省内存。
""",
    },
    {
        "title": "memorys 检索策略：为什么用 RRF 融合",
        "type": "decision",
        "project": "memorys",
        "tags": ["search", "jieba", "rrf", "pgvector"],
        "importance": 4,
        "source": "agent:hermes",
        "content": """# 决策

中文检索不能只靠 PostgreSQL 默认分词（没装 zhparser，`to_tsvector('simple', ...)` 对中文等于按字切）。
最终方案是多路召回 + RRF 融合：

1. **jieba `cut_for_search` 分词** → 生成 lexeme 存 tsvector，查询时同样分词后用 OR 连接。
   `tokenize()` 是双向桥：写路径生成索引词，读路径解析查询词。
2. **pg_trgm `similarity() > 0.2`** 兜底错别字和部分词。
3. **embedding 向量**（可选）：`MEM_EMBED_API_BASE` 留空就纯关键词，配了 OpenAI 兼容端点自动启用。
4. **RRF 融合**：`score += 1.0 / (60 + rank + 1)`，避免不同策略分数不可比。

## 为什么不默认开向量

默认零依赖零成本就能覆盖大部分场景，向量要外部 API Key 和调用成本。
需要语义检索时再配 embedding 端点，代码路径已经留好。

## 检索粒度

md 按标题层级切 chunk 建索引，命中返回 chunk + 所属文档 id/title，需要全文再调 `memory_get(id)`。
避免一次返回超长文档打爆 context。
""",
    },
    {
        "title": "memorys 运维要点：systemd + nginx + 宝塔 WAF",
        "type": "howto",
        "project": "memorys",
        "tags": ["deploy", "nginx", "systemd", "btwaf"],
        "importance": 5,
        "source": "agent:hermes",
        "content": """# 服务

`systemctl {status,restart} memorys` ，日志 `/var/log/memorys.log` 。
监听 127.0.0.1:8649，**必须单 worker**：MCP 的 session_manager 和 contextvar 认证是进程内状态，多 worker 会让会话在 worker 间漂移。
要扩容就 nginx 上游挂多实例 + 各自端口。

## nginx

vhost `/www/server/panel/vhost/nginx/repo.xlingo.fun.conf` 反代到 8649。
`/mcp` 这个 location **必须关掉 proxy_buffering / proxy_request_buffering**，否则 MCP 的 SSE 流式响应会被缓冲住。
改配置前的原站备份：`repo.xlingo.fun.conf.bak-before-memorys-20260831-194336` 。

## 宝塔 WAF

大请求体（整篇项目总结）会被误拦成 403。已在 `/www/server/btwaf/rule/url_white.json` 加白名单：
`^/mcp`、`^/api/v1/documents`、`^/api/v1/sync` 。改前备份 `url_white.json.bak-memorys-20260831-202447` 。

## MCP 的 DNS rebinding 坑

FastMCP 见后端只听 127.0.0.1 就只放行本机 Host，经 nginx 进来的真实域名会被判成 DNS rebinding 直接返回 421。
必须在 `MEM_MCP_ALLOWED_HOSTS` 显式放行域名。

## 另外两个坑

- Starlette 的 Mount 对 `/mcp`（无尾斜杠）会 307 重定向到 `/mcp/`，POST 带 body 时客户端不一定跟随 → 配置里直接写 `/mcp/` 。
- `vector` 扩展要在**目标库**里单独 `CREATE EXTENSION`，不是装一次全局通用。
""",
    },
    {
        "title": "memorys GitHub 备份：多用户分支隔离",
        "type": "decision",
        "project": "memorys",
        "tags": ["git", "github", "backup"],
        "importance": 4,
        "source": "agent:hermes",
        "content": """# 问题

最初 `MEM_GITHUB_REMOTE` 和 `MEM_SYNC_BRANCH` 都是全局配置，多个用户的 repo 会推到同一个 remote 的同一个分支，互相覆盖。

# 方案

`MEM_GITHUB_REMOTE` 支持 `{uid}` 占位符：

- 带 `{uid}`（如 `git@github.com:me/memorys-u{uid}.git`）= 一人一仓，分支用 `MEM_SYNC_BRANCH`
- 不带 `{uid}`（如 `git@github.com:me/memorys-data.git`）= 多用户共用一仓，自动按 `u<uid>` 分支隔离

推送时显式写 `HEAD:refs/heads/<branch>`，保证落到该用户自己的远端分支。
已用本地 bare 仓库实测：两个用户推同一个仓库，远端出现 `u901`/`u902` 两个独立分支，各自只有自己的文档。

# 注意

remote 必须用 `git@` 形式（SSH 免密已配好）。写 `https://` 会报 `could not read Username` —— 那是协议选错，不是没登录。
GitHub 上的仓库需要先手动建好，服务不会自动创建；远端不存在时 `/api/v1/sync/push` 返回可读错误而不是崩溃。
""",
    },
    {
        "title": "如何把 memorys 挂给 AI agent 用",
        "type": "howto",
        "project": "memorys",
        "tags": ["mcp", "hermes", "agent", "onboarding"],
        "importance": 5,
        "source": "agent:hermes",
        "content": """# 拿 Key

登录 https://repo.xlingo.fun → 「🔑 API Key」→ 新建，得到 `hk_` 开头的 Key（只显示一次）。

# Hermes

```
hermes config set mcp_servers.memorys.url https://repo.xlingo.fun/mcp/
hermes config set mcp_servers.memorys.headers.X-Api-Key hk_你的key
hermes mcp test memorys
```

# Claude Desktop / Cursor

```json
{"mcpServers": {"memorys": {"type": "http", "url": "https://repo.xlingo.fun/mcp/",
  "headers": {"X-Api-Key": "hk_你的key"}}}}
```

# 新会话开场怎么用

调 `memory_bootstrap(project="项目名")` 一次性取回该项目的压缩上下文包（项目总结 → 决策/偏好 → howto → 术语 → 事实，
按 importance 排序，在 token 预算内截断），直接塞进开场即可恢复工作上下文。

# 写入约定

- `type` 选对：`project_summary`（架构/交接）/ `decision`（为什么这么选）/ `preference`（用户偏好）/ `howto`（操作步骤）/ `glossary` / `fact`
- `importance` 1-5，直接影响 bootstrap 的优先级，交接类信息给 5
- 撞同名标题默认报冲突并返回已存在的 id，要覆盖就传 `mode="upsert"`，要追加就 `memory_update(mode="append")`
""",
    },
]


def main() -> int:
    ok, fail = [], []
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        me = c.get(f"{BASE}/api/v1/auth/me", headers=H)
        print("auth/me:", me.status_code, me.text[:200])
        if me.status_code != 200:
            print("认证失败，中止")
            return 1

        for d in DOCS:
            r = c.post(f"{BASE}/api/v1/documents", headers=H, json=d)
            if r.status_code in (200, 201):
                ok.append((r.json().get("id"), d["title"]))
            elif r.status_code == 409:
                ok.append(("已存在", d["title"]))
            else:
                fail.append((r.status_code, d["title"], r.text[:200]))

        print("\n写入成功:")
        for i, t in ok:
            print(f"  {i}  {t}")
        if fail:
            print("\n写入失败:")
            for s, t, e in fail:
                print(f"  {s}  {t}  {e}")

        b = c.get(f"{BASE}/api/v1/bootstrap", headers=H,
                  params={"project": "memorys", "token_budget": 4000})
        bj = b.json()
        print(f"\nbootstrap(memorys): {b.status_code} "
              f"{bj.get('document_count')}/{bj.get('total_documents')} 篇 "
              f"~{bj.get('estimated_tokens')} tokens")

        for q in ["memorys 怎么部署", "为什么用 RRF", "怎么挂给 agent 用", "WAF 403 怎么办"]:
            r = c.get(f"{BASE}/api/v1/search", headers=H, params={"q": q, "limit": 3})
            titles = [h["doc"]["title"] for h in r.json().get("results", [])]
            print(f"search[{q}] -> {titles}")

        p = c.post(f"{BASE}/api/v1/sync/push", headers=H)
        print(f"\nsync/push: {p.status_code} {json.dumps(p.json(), ensure_ascii=False)[:300]}")

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
