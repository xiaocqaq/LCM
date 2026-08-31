# memorys — AI 跨会话知识库

给人和 AI agent 共用的记忆库。换模型、换 agent、换会话之后，仍然能把项目上下文一次性读回来。

线上地址：**https://repo.xlingo.fun**

- Web UI：`https://repo.xlingo.fun/`
- REST API 文档：`https://repo.xlingo.fun/api/docs`
- MCP 端点：`https://repo.xlingo.fun/mcp`（streamable HTTP）

---

## 设计要点

**Markdown 是唯一事实来源，PG 只是索引。**
每条记忆是一个带 YAML frontmatter 的 `.md` 文件，落在 `/var/lib/memorys/data/users/u<id>/<库>/*.md`。
数据库随时可以丢弃重建（`POST /api/v1/sync` 扫盘回灌）。这条约束换来两个好处：数据可以直接用编辑器看/改，也不会被某个 schema 变更绑死。

**每个用户一个独立 git 仓库。**
写入即 commit，删除是软删除（文件移到 `.trash/`，git 有记录）。`GET /api/v1/documents/{id}/history` 直接读 git log。可选推 GitHub 做长期备份，每个用户推到自己的分支 `u<id>`。

**用户隔离在服务端强制。**
所有查询都带 `user_id` 条件，路径拼接前做 slug 消毒。已实测：另一个用户看不到文档、按 id 直取返回 404、检索 0 命中。

**认证复用 ai.xlingo.fun，本地不存密码。**
三种凭据都能用：

| 凭据 | 用途 | 说明 |
|---|---|---|
| 本地 JWT | Web UI | `POST /api/v1/auth/ai-login` 用账号密码换取，12h 有效 |
| ai.xlingo.fun 的 accessToken | 已登录上游的场景 | 共享同一 HS256 secret，可直接验签；首次使用会回调上游 `/api/v1/me` 复核后自动开户 |
| API Key（`hk_` 开头） | agent / 脚本 / MCP | Web UI「API Key」页创建，只显示一次，sha256 存库 |

密码校验、账号锁定、2FA、审计全部在上游完成。

**检索是三路融合（RRF），中文友好。**
jieba 分词 → `tsvector` 关键词召回；`pg_trgm` 相似度兜底错别字和部分词；embedding 向量可选（`MEM_EMBED_API_BASE` 留空则不启用）。三路结果按 `score += 1/(60+rank+1)` 融合。
没有用 PG 内置中文分词——本机没装 zhparser，内置分词对中文基本无效。

**索引侧扩展、查询侧不扩，这个不对称是刻意的。**
问题出在正文和提问的用词根本不是一套：正文写具体形态（`/var/lib/memorys/data/users/`、`8649`、`systemd`），
人提问用概念词（"记忆文件存在哪个目录"）。纯关键词检索下两边零重叠，必然 miss——实测过，确实一条都不召回。

解法是只把索引"摊开"（`app/search.py:expand_tokens`）：

- 正文里出现真实路径（`(/[\w.\-]+){2,}`）→ 注入 `目录/路径/文件夹/位置`，路径段单独切词并剔掉 `var/lib/opt` 这类噪声
- 英文技术词 → 中文概念词：`data→数据`、`systemd→服务/开机自启`、`nginx→反代/网关`、`git→版本/提交`
- 中文概念同义词链：`目录→路径/文件夹/存放/位置`、`鉴权→认证/登录/密钥`

查询侧 `tokenize()` 保持原样不扩。反过来扩查询会把无关文档拉进结果，
而单向扩展补了召回又不误召：`养猫要注意什么`、`今天天气怎么样`、`股票行情` 实测仍是 0 命中。

这套契约由 `tests/search_unit_test.py`（26 项）和 `tests/recall_public.py`（11 项打公网）锁住。
改 `search.py` 后必须重跑，并且要 `POST /api/v1/sync {"action":"reindex"}` 全量重建索引——
扩展发生在写入时，光改代码不重建索引对存量文档无效。

---

## MCP 接入

先在 Web UI 的「API Key」页建一个 key，然后配到客户端。

Claude Desktop / Cursor：

```json
{
  "mcpServers": {
    "memorys": {
      "type": "http",
      "url": "https://repo.xlingo.fun/mcp",
      "headers": { "X-Api-Key": "hk_你的key" }
    }
  }
}
```

10 个工具：

| 工具 | 作用 |
|---|---|
| `memory_bootstrap` | **新会话开场先调这个**，按 token 预算返回项目压缩上下文 |
| `memory_search` | 中文检索，返回命中的章节 + 所属文档 |
| `memory_get` | 按 id 读全文 |
| `memory_write` | 写入（`mode=upsert` 可覆盖同名） |
| `memory_update` | 改已有文档 |
| `memory_delete` / `memory_restore` | 软删除 / 恢复 |
| `memory_list_libraries` / `memory_list_docs` | 列库 / 列文档 |
| `memory_history` | 读 git 变更历史 |

典型用法：新会话开场调 `memory_bootstrap(project="xspeak")` 拉回上下文，干完活调 `memory_write` 把结论写回去。

---

## REST 速查

```bash
# 检索（q 是必填，留空返回 400）
curl -H "X-Api-Key: $KEY" "https://repo.xlingo.fun/api/v1/search?q=xspeak 怎么发版"

# 上下文包（project 留空 = main 库全部）
curl -H "X-Api-Key: $KEY" "https://repo.xlingo.fun/api/v1/bootstrap?project=xspeak&token_budget=4000"

# 写入
curl -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"title":"标题","type":"decision","content":"正文","project":"xspeak","importance":5}' \
  https://repo.xlingo.fun/api/v1/documents
```

`type` 取值：`project_summary` / `decision` / `preference` / `howto` / `glossary` / `fact`。
`importance` 1-5，影响 `bootstrap` 的挑选优先级。

---

## 运维

```bash
systemctl status memorys
systemctl restart memorys
tail -f /var/log/memorys.log
```

| 项 | 值 |
|---|---|
| 代码 | `/opt/memorys` |
| 数据（md + git） | `/var/lib/memorys/data/users/u<id>/` |
| 端口 | `127.0.0.1:8649`（nginx 反代） |
| systemd | `/etc/systemd/system/memorys.service` |
| nginx | `/www/server/panel/vhost/nginx/repo.xlingo.fun.conf` |
| 数据库 | `postgresql://memorys@127.0.0.1:54321/memorys` |
| 日志 | `/var/log/memorys.log` |

**单 worker 是故意的**：MCP 的 session_manager 和 contextvar 认证都是进程内状态，多 worker 会让 MCP 会话在 worker 之间漂移。要扩容就起多实例 + nginx 上游轮询。

**nginx 里 `/mcp` 单独一段**：MCP 响应是 SSE 流，必须 `proxy_buffering off`，否则客户端一直等不到数据。

**btwaf 白名单**：`/www/server/btwaf/rule/url_white.json` 里已加 `^/mcp`、`^/api/v1/documents`、`^/api/v1/sync`——宝塔防火墙会把大请求体误判成攻击回 403。

### 灾恢

md 文件是事实来源，DB 丢了不影响数据：

```bash
curl -X POST -H "X-Api-Key: $KEY" https://repo.xlingo.fun/api/v1/sync        # 扫盘重建索引
curl -X POST -H "X-Api-Key: $KEY" https://repo.xlingo.fun/api/v1/sync/push   # 推 GitHub 备份
```

### GitHub 备份（可选，未启用）

`.env` 里已配 `MEM_GITHUB_REMOTE=git@github.com:xiaocqaq/memorys-data.git`，但仓库还没建，所以推送会返回 `ok:false` 并提示。本机 SSH 免密已通（账号 `xiaocqaq`），去 GitHub 建一个同名私有空仓库就能用。

---

## 测试

一键跑全部七套 + 服务状态 + 公网端点 + Hermes 侧闭环：

```bash
bash /opt/memorys/tests/acceptance.sh
```

单独跑（`PYTHONPATH` 不能省，否则 `import app.*` 失败）：

```bash
cd /opt/memorys && export PYTHONPATH=/opt/memorys
.venv/bin/python tests/search_unit_test.py      # 分词/扩展契约 26 项（纯本地，不需要服务）
.venv/bin/python tests/recall_public.py         # 中文召回质量 11 项（打公网）
.venv/bin/python tests/e2e_test.py              # REST 全链路 26 项
.venv/bin/python tests/mcp_test.py              # MCP 14 项
.venv/bin/python tests/upstream_token_test.py   # 上游 token 路径 9 项
.venv/bin/python tests/public_mcp_test.py "$(cat /root/.memorys-hermes-key)"  # 公网 MCP 6 项
.venv/bin/python tests/git_push_test.py         # GitHub 备份链路 13 项（本地 bare 仓库，不碰真远端）

# 把任意一套指向公网域名
MEM_TEST_BASE=https://repo.xlingo.fun .venv/bin/python tests/e2e_test.py
```

测试会造 `memtest` / `boundtest` 这类用户（`xiaoai_user_id` 落在 999000-999999）。
清理用定向删除，**不要 `TRUNCATE users CASCADE`**——那会把真实账号一起清掉：

```sql
BEGIN;
DELETE FROM chunks WHERE document_id IN (
  SELECT id FROM documents WHERE user_id IN (
    SELECT id FROM users WHERE xiaoai_user_id BETWEEN 999000 AND 999999));
DELETE FROM documents WHERE user_id IN (
  SELECT id FROM users WHERE xiaoai_user_id BETWEEN 999000 AND 999999);
DELETE FROM users WHERE xiaoai_user_id BETWEEN 999000 AND 999999;
COMMIT;
```

配套删数据目录（保留真实用户 `u20`）：

```bash
cd /var/lib/memorys/data/users && for d in u*; do [ "$d" = u20 ] || rm -rf "$d"; done
```

DB 和磁盘要一起清。只清 DB 会留下孤儿 md，下次 `sync` 会被重新索引回来。

---

## 踩过的坑

1. `mcp` 库必须钉 `<2`。2.x 把 `FastMCP` 改名 `MCPServer`，接口大改。
2. `mcp.streamable_http_app()` 挂载后，必须在 FastAPI lifespan 里 `async with session_manager.run()`，否则报 `Task group is not initialized`。
3. Starlette 的 `Mount("/mcp")` 只匹配 `/mcp/…`，裸 `POST /mcp` 会被 `redirect_slashes` 回 307，而 MCP 客户端不跟重定向（跟了也会丢 body）。解法是加一层 ASGI 中间件在进路由前把路径补成 `/mcp/`（见 `main.py` 的 `McpSlashMiddleware`）。
4. SQLAlchemy `text()` 里不能写 `:param::text`，会报 `syntax error at or near ":"`；要写 `CAST(:param AS text)`。
5. asyncpg 推不出 `:lib IS NULL` 的类型，报 `AmbiguousParameterError`，同样用 `CAST` 解决。
6. `vector` 扩展要在**目标库**里单独装，不是装一次全局通用。
7. `session.commit()` 之后访问 ORM 属性可能触发 `MissingGreenlet`，需要 `await session.refresh(obj)`。
8. 跑测试脚本必须带 `PYTHONPATH=/opt/memorys`。
9. FastAPI 的 `root_path` 会连带影响 `app.mount()` 的匹配前缀。nginx 已经剥掉了路径前缀，再设 `root_path` 会让 `/mcp` 变 404。所以不设 `root_path`，Swagger 的 `openapi_url` 改用自定义 `/api/docs` 路由手工拼前缀。
10. `FastMCP` 在 host 为 `127.0.0.1` 时会自动开 DNS rebinding 防护，经域名反代进来直接 421。要显式给 `TransportSecuritySettings` 配 `allowed_hosts`/`allowed_origins`（对应 `MEM_MCP_ALLOWED_HOSTS`）。
11. slug 去重必须把**软删除的行**算进去。`rel_path` 唯一索引不区分删除状态，漏算会在重建同名文档时撞 `IntegrityError` 500。
12. `_tsvector_literal` 的产物是 `to_tsvector()` 的输入，也就是普通文本，词之间用空格分隔。写成 `'a | b'` 会让管道符自己变成一个 lexeme。`|` 只在 `to_tsquery` 那侧用。
13. 检索加了 `PER_DOC_CAP = 2`，避免一篇长文档吃满整个结果集，命中不足时再 spill 补齐。
14. 断言别依赖失败文案。`memory_search` 命中时返回结构化 JSON，"没有命中"只在 0 命中时出现，写 `"命中" in text` 会永远为真。
15. 测试标题要带时间戳后缀，否则第二轮跑会撞 409 duplicate。
