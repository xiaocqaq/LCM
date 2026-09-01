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
Web UI 的「MCP 配置」页会把下面这些样例连同真实 URL 一起渲染出来，可以直接复制。

**这是远程 HTTP 服务，不是本地程序，所以配置里只有 URL，没有 `command`。**
写了 `command`（哪怕是空串）客户端就会按"启动本地进程"处理，报
`program path has no file name`。这是最常见的配错方式。

Claude Desktop / Cursor / VS Code — JSON：

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

放置位置：Claude Desktop `%APPDATA%\Claude\claude_desktop_config.json`；
Cursor `.cursor/mcp.json`；VS Code `.vscode/mcp.json`。

**Codex CLI 只认 TOML**，改 `~/.codex/config.toml`：

```toml
[mcp_servers.memorys]
url = "https://repo.xlingo.fun/mcp"

[mcp_servers.memorys.http_headers]
X-Api-Key = "hk_你的key"
```

`http_headers` 必须是独立的一段，不能内联进上面那段（内联会报 `invalid transport`）。
`codex mcp get memorys` 应显示 `transport: streamable_http`。

Codex 的实测边界（0.151.0）：

| 写法 | 结果 |
|---|---|
| 只有 `url` | ✅ |
| `url` + `[...http_headers]` | ✅ |
| `url` + `command = ""` | ❌ 配置直接加载失败 |
| 只有 `command = ""` | ⚠️ 配置能过，启动时报 `program path has no file name` |
| `url` + `args` | ❌ 配置加载失败（别混入 stdio 字段） |
| `.mcp.json` | ❌ Codex 不读，试过 7 种开关都不行 |

Hermes — 用 CLI 写，别手改 config.yaml：

```bash
hermes config set mcp_servers.memorys.url https://repo.xlingo.fun/mcp
hermes config set mcp_servers.memorys.headers.X-Api-Key hk_你的key
hermes mcp test memorys      # 应报 ✓ Connected + Tools discovered: 10
```

认证只吃 `X-Api-Key`。`Authorization: Bearer <api key>` 会 401 —— Bearer 那条通道
是给网页登录的 JWT 用的，两者不通用。URL 尾斜杠可有可无（`McpSlashMiddleware` 已兜住）。

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

### 分支（Web UI「🌿 分支」页）

UI 上分支切换走下拉（选中即切），合并也是下拉选源；合并源下拉会自动排掉当前分支。
用户取消确认时下拉会回弹到实际分支——显示值和真实所在分支不一致比切换失败更危险。

想大改记忆又不想动主线时用。每个用户的 repo 独立，分支只在自己库内。

```
GET    /api/v1/branches                     列出分支 + 当前分支 + 工作区是否干净
POST   /api/v1/branches         {name,switch}   建（默认建完就切）
POST   /api/v1/branches/switch  {name}          切
POST   /api/v1/branches/merge   {name,message}  把 name 合进当前分支
DELETE /api/v1/branches/{name}[?force=true]     删
```

三个设计取舍：

**切换/合并前自动 commit**。脏工作区切分支会把未提交改动带过去，造成内容归属混乱；
自动落一笔 `wip:` 提交最省心，也不会丢东西。

**切换/合并后必须重建索引**。这两个操作直接改磁盘上的 md，PG 里还是旧的。
UI 已经自动接上 reindex；走 REST 的话要自己补一次 `POST /api/v1/sync {"action":"reindex"}`。

**合并冲突不自动解决**。失败就 `merge --abort` 回滚，返回 hint 让人工处理，
绝不留半成品在工作区。

分支名走白名单校验（`^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$`，额外挡掉 `..`、`@{`、
`/` 结尾、`.lock` 结尾）。这些值要拼进 git 命令，`--force` 之类的输入必须挡在门外。
主分支和当前所在分支不允许删除。

### 写记忆时直接选分支

编辑页顶部有「保存到」下拉，可以把这一篇写到别的分支（下拉里还能现场新建）。
REST 和 MCP 也支持：

```bash
# 新建并写到 draft 分支（分支不存在会自动创建）
curl -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"title":"草稿","type":"fact","content":"..."}' \
  "$BASE/api/v1/documents?branch=draft"

# 已有文档"另存"到分支：原文档在当前分支保持不动
curl -X PATCH -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"content":"改写版"}' "$BASE/api/v1/documents/123?branch=draft"
```

MCP：`memory_write(..., branch="draft")`。

**写别的分支时不落盘、不进 DB，这是刻意的。**
磁盘 md 和 PG 索引都是"当前分支"的平铺视图。如果写别的分支时也落盘，工作区就
变成两个分支的混合体；如果也进 DB，检索会返回当前分支上根本不存在的文档。
所以这条路径只往 git 对象库提交，等你切过去或合并过来，reindex 自然会收录。

实现上**没有用 `checkout` 来回切**——那要翻动两次工作区、重建两次索引，
中途任何一步失败都会把你留在错误的分支上。走的是 git plumbing：
`hash-object` 造 blob → 临时 `GIT_INDEX_FILE` + `read-tree`/`update-index` 拼 tree
→ `commit-tree` → `update-ref` 挪分支指针。工作区和当前分支全程一动不动
（`git status --porcelain` 实测仍为空）。

同一分支上重复写同路径会识别成 update 并保留原 `created_at`。
目标分支写成当前分支会返回 400（那种情况直接正常保存即可）。

### GitHub 备份（已启用）

远端 `git@github.com:xiaocqaq/memorys-data.git`（private），走 SSH 免密（账号 `xiaocqaq`）。
每个用户推到自己的分支（`u<id>`），互不覆盖——见上文「分支」节的 `remote_for`/`branch_for`。

手动推：Web UI「同步 / GitHub」页点「立即推送」，或 `POST /api/v1/sync/push`。

**定时推送：每天 01:00**，systemd timer：

```
/etc/systemd/system/memorys-push.timer     # OnCalendar=01:00，RandomizedDelaySec=180，Persistent=true
/etc/systemd/system/memorys-push.service   # oneshot
/opt/memorys/deploy/memorys-push.sh        # 实际逻辑
```

```bash
systemctl list-timers memorys-push.timer   # 看下次触发
systemctl start memorys-push.service       # 手动触发一次
journalctl -u memorys-push -n 30           # 看日志
```

UI 的「同步 / GitHub」页会显示下次推送时间和上次结果（读 `GET /api/v1/sync/schedule`，
底层是 `systemctl show`）。

三个设计取舍：

**脚本走 REST 而不是直接 `git push`。** 推送逻辑在 `gitsvc.sync_to_github` 里
（分支按用户隔离、`{uid}` 占位符替换、`HEAD:refs/heads/<branch>` 的写法）。
绕过它自己拼 git 命令就有两份逻辑，将来改一处忘一处。走 REST 打的是同一个入口，
行为和网页上点「立即推送」完全一致。

**推送前先 reindex。** 直接改过磁盘上的 md 时，这一步能把改动带上。

**失败不重试**（`Restart=no`）。timer 明天照常跑，重试只会刷满日志。
`Persistent=true` 负责补跑：机器关机错过了，开机后会补一次，不然一停机就断档。

多用户场景：key 写进 `/etc/memorys/push-keys`（每行一个，`#` 注释），
脚本会逐个推。没有该文件时退回读 `/root/.memorys-hermes-key`。

**验证备份真的可用**——别只看接口返回的 `ok:true`，从远端 clone 下来看：

```bash
git clone --depth 1 --branch u20 git@github.com:xiaocqaq/memorys-data.git /tmp/vfy
ls /tmp/vfy/main/ && head -8 /tmp/vfy/main/*.md
```

---

## 测试

一键跑全部九套 + 服务状态 + 公网端点 + Hermes 侧闭环：

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
.venv/bin/python tests/branch_test.py           # 分支管理 23 项（建/切/合/删 + 非法输入）
.venv/bin/python tests/branch_write_test.py     # 写入指定分支 25 项（隔离性 + 边界）
bash tests/cleanup.sh                           # 测试跑完清残留（分支/文档/md/测试账号）

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
16. **反向断言（"这个查询不该命中"）不能打真实库**。`recall_public.py` 里曾用「养猫要注意什么」，
    后来库里进了一篇带「工作区注意事项」的技术文档，「注意」真实命中 → 断言误报失败。
    打真实库的反向用例要选库里绝不会出现的词（`孜然羊肉`、`演唱会门票`）；
    对固定样例常量断言的单测（`search_unit_test.py`）才可以用通用词。
17. **切分支/合并后一定要 reindex**。md 换了但 PG 索引没换，会搜到已经不在磁盘上的内容。
18. 分支名会拼进 git 命令，必须白名单校验。`--force`、`a..b`、`x@{1}` 这类输入
    不挡住就会变成 git 的 flag 或保留语法。
19. **往别的分支写文件不要用 `checkout` 来回切**。用 git plumbing
    （`hash-object` → 临时 `GIT_INDEX_FILE` + `read-tree`/`update-index` → `commit-tree`
    → `update-ref`），工作区零改动。`GIT_INDEX_FILE` 必须指向临时文件，
    用默认 index 会把工作区的暂存状态搅乱。
20. **UI 里"先写提示再刷新下拉"会白写**。`loadEditBranches()` 内部会调
    `updateBranchHint()` 覆写 `#brhint`，所以要先 `await` 刷新、再写结果文案。
    （踩过一次：保存成功但提示区是空的）
