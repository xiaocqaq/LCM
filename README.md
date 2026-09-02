# LCM — memorys

面向 **AI 跨会话使用** 的个人知识库 / 记忆服务。

换模型、换 agent、开新会话时，从这里一次性取回项目上下文，不用重新解释一遍。

- **Markdown 是 source of truth**，PostgreSQL 只做索引 —— 数据库随时可以从磁盘全量重建
- **每用户独立 git 仓库**做版本化，可定时推 GitHub 长期备份
- **中文友好的混合检索**：jieba 分词 tsvector + pg_trgm 模糊兜底 + 可选向量语义（三路 RRF 融合）
- **三种接入形态**：REST API、MCP Server（streamable HTTP）、Web UI
- 可复用已有站点的账号体系登录，也可以纯 API Key

## 这是什么问题

跟 agent 聊了两小时把项目讲清楚，明天换个模型，一切从零开始。
把结论写进文档也没用 —— agent 不知道去哪儿找，也不知道哪些跟当前任务相关。

所以需要的不是"文件存储"，而是：**agent 能自己写、自己搜、自己按 token 预算取回的记忆层**。

`memory_bootstrap` 就是为这个场景做的：给一个项目名和 token 预算，
返回按重要度和时间排好序、装箱到预算内的上下文包，直接塞进新会话开场。

## 快速开始

```bash
git clone https://github.com/xiaocqaq/LCM.git memorys
cd memorys

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# 编辑 .env：至少填 MEM_DATABASE_URL 和 MEM_JWT_SECRET
#   openssl rand -hex 32   # 生成 JWT secret

.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8649
```

数据库需要 PostgreSQL 加两个扩展：

```sql
CREATE DATABASE memorys;
\c memorys
CREATE EXTENSION IF NOT EXISTS vector;    -- 向量检索用，不用向量也建议装
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- 模糊匹配兜底，必需
```

缺 `MEM_DATABASE_URL` 或 `MEM_JWT_SECRET` 会直接启动失败并提示 —— 
这是刻意的，空 secret 签出的 JWT 谁都能伪造，而服务照样返回 200。

## MCP 接入

服务起来后，端点是 `http://127.0.0.1:8649/mcp`（streamable HTTP）。
先建一个 API Key：

```bash
PYTHONPATH=. .venv/bin/python tests/mint_key.py
```

**Claude Desktop / Cursor / VS Code**（`.mcp.json` 或对应配置）：

```json
{
  "mcpServers": {
    "memorys": {
      "url": "https://your-domain.example.com/mcp",
      "headers": { "X-Api-Key": "hk_xxx" }
    }
  }
}
```

**Codex CLI**（`~/.codex/config.toml`）—— 注意 Codex 只认 TOML，不读 `.mcp.json`，
且 `http_headers` 必须是独立的表段，写成内联会报 `invalid transport`：

```toml
[mcp_servers.memorys]
url = "https://your-domain.example.com/mcp"

[mcp_servers.memorys.http_headers]
X-Api-Key = "hk_xxx"
```

远程 HTTP 型 MCP **没有 `command`**。写了 `command` 客户端会走 stdio 分支，
报 `program path has no file name`。stdio（本地可执行文件）和 streamable HTTP
（远程服务）是两种传输，客户端靠 `command`/`url` 哪个存在来判断。

### 十个工具

| 工具 | 用途 |
|---|---|
| `memory_bootstrap` | **换会话时先调这个**：按 token 预算取回项目上下文包 |
| `memory_search` | 混合检索，返回 chunk + 所属文档元信息 |
| `memory_get` | 按 id 取全文 |
| `memory_write` | 写入新记忆（含相似检测，避免重复堆积） |
| `memory_update` | 更新已有记忆 |
| `memory_delete` / `memory_restore` | 软删除与回收站恢复 |
| `memory_list_libraries` | 列出知识库 |
| `memory_list_docs` | 按条件列标题（不含正文，省 token） |
| `memory_history` | 看一篇记忆的 git 变更历史 |

## 文档结构

每篇记忆是一个带 YAML frontmatter 的 md 文件：

```markdown
---
id: mem_01h9x...
title: xspeak 项目架构总结
type: project_summary     # project_summary | decision | preference | howto | glossary | fact
project: xspeak
tags: [nextjs, postgres]
importance: 5             # 1-5，影响 bootstrap 的取回优先级和检索排序
created_at: 2026-09-01T10:00:00+08:00
updated_at: 2026-09-01T10:00:00+08:00
source: agent:claude      # 谁写的
---

# 正文
```

磁盘布局：

```
$MEM_DATA_DIR/users/u<uid>/<branch>/*.md
```

每个用户目录是独立 git 仓库。写入即 commit，改错了能 `memory_history` 查、能回滚。

## 检索设计

三路并行召回，RRF 融合，再做两道后处理。

**第一路 · 关键词**：jieba `cut_for_search` 切词后进 `to_tsvector('simple', ...)`。
没用 zhparser，因为它要额外装扩展，而 jieba 在应用层切完效果够用。

**第二路 · 模糊兜底**：`pg_trgm` 的 `similarity() > 0.2`，抗错字和局部匹配。

**第三路 · 向量语义**（配了 `MEM_EMBED_API_BASE` 才启用）：
任意 OpenAI 兼容 `/embeddings` 端点。没配就自动降级成两路，功能完整。

### 索引侧单向同义扩展

这是本项目里比较有意思的一个设计。

正文里写的是具体形态（`/var/lib/memorys`、`54321`、`systemctl restart`），
提问用的是概念词（"数据存哪"、"端口多少"、"怎么重启"）。两边对不上。

做法是**只在索引侧扩展，查询侧不扩**：建索引时把「目录」也写进「路径/文件夹/存放/位置」，
查询时按原词匹配。这样补了召回又不误召 —— 双向扩展会让「养猫注意事项」
命中一堆技术文档，单向不会。实测反向用例 0 误命中。

### 长度归一化

RRF 只看排名不看体量。实测 17 个查询，一篇 15303 字符的长文出现在 13 个结果里、
7 次排第一 —— 它长度是其他文档的 6-230 倍，词汇覆盖面天然大，
靠"碰巧含有查询词"就挤掉真正对题的短文。

补了 `(avg_len/doc_len)^0.35`，只对超过均长的文档衰减。指数压到 0.35 是刻意的：
目的是抵掉体量优势，不是把长文赶出结果（长文往往信息也多）。
BM25 用 `b` 参数做这件事，`ts_rank` 没有。

| | 修前 | 修后 |
|---|---|---|
| 长文出现次数 | 13/17 | 5/17 |
| 长文排第一 | 7 次 | 2 次 |

另加 importance 加权 ±10%，同分时高重要度靠前，权重压得小以免盖过相关性。

`tests/rank_test.py` 锁住三条：对题短文要排在长文前、同长度时 importance 高的靠前、
**长文在自己真正对题的查询上仍要排第一**（最后这条是防止惩罚过头的平衡点）。

### 向量检索值不值得开

实测数据（`tests/ab_vector.py`，15 个查询）：

| | 两路 | 三路 |
|---|---|---|
| top5 命中率 | 80% | **100%** |
| top1 命中率 | 80% | 73% |

向量救回的都是纯概念提问 —— 比如问「数据存在哪里」，正文写的是
"Markdown 是 source of truth"，一个词都对不上。这类查询同义词表补不了
（试过，加 5 条同义词仍然 0/3，还把原本对的结果搞乱）。

但 top1 掉了 7 个点，且代价不小：每次检索多一次网络往返、全量 reindex 从秒级变成分钟级、
多一个会挂的外部依赖。

**小库（百篇以内、纯技术文档）不开也完全够用。** 值得开的信号：
文档过 300 篇、或开始存日记/会议记录这类词汇发散的内容。

配置示例见 `.env.example`。注意 `MEM_EMBED_DIM` **必须**和上游实际返回的维度一致 ——
hnsw 索引建在固定维度的表达式上，填错不报错而是索引静默失效。

## Web UI

- 项目文件夹树（按 type 分组，折叠状态本地保存）
- 全文搜索、编辑、软删除与恢复
- 图形化 git 分支管理：新建 / 下拉切换 / 合并 / 删除
- 写入时可选目标分支（写非当前分支走 git plumbing，工作区零改动）
- bootstrap 预览：调预算看实际取回什么
- API Key 管理、MCP 配置速查
- 亮/暗双主题，未设置时跟随系统

主题对比度按 WCAG 实测校准过，不是目测：

| | 亮色 | 暗色 | 要求 |
|---|---|---|---|
| 文档标题 | 11.53:1 | 10.92:1 | 4.5 |
| 分组标题 | 4.96:1 | 5.79:1 | 4.5 |
| 实星 | 4.09:1 | 7.72:1 | 3 |
| 空星 | 3.30:1 | 3.45:1 | 3 |

## 认证

两条路径共用一个 HS256 secret：

| 凭据 | 场景 | 校验方式 |
|---|---|---|
| API Key `hk_xxx` | agent、脚本、MCP | bcrypt 比对，记录 `last_used_at` |
| 本服务签的 JWT | Web UI 登录后 | `iss=memorys`，本地验签 |
| 上游站点的 accessToken | 已登录上游的场景 | 共享 secret 直接验签；首次使用回调上游 `/api/v1/me` 复核后自动开户 |

最后一条的细节值得说明：**验签有效 ≠ 会话还在**。用户可能已经在上游登出，
但 token 还没过期。所以首次绑定时必须回调上游确认会话状态，不能只看签名。

密码不落本地。`POST /api/v1/auth/ai-login` 把凭据转发给上游，
按上游 user id upsert 本地账号。

## 部署

参考 `deploy/` 下的模板：

| 文件 | 用途 |
|---|---|
| `memorys.service` | systemd unit |
| `nginx-*.conf.snippet` | nginx 反代片段（含 MCP 的 SSE 相关设置） |
| `memorys-push.{sh,service,timer}` | GitHub 备份定时推送 |
| `scrub-history.sh` | 开源前清洗 git 历史里的密钥 |

几个部署要点：

**单 worker**。MCP 的 session_manager 和认证用的 contextvar 都是进程内状态，
多 worker 会串。

**反代子路径**要设 `MEM_ROOT_PATH`。FastAPI 的 `root_path` 会破坏 `app.mount()`，
所以本项目用配置项自己处理，没直接传给 FastAPI。

**MCP 的 Host 白名单**。FastMCP 在 `host=127.0.0.1` 时会自动开 DNS rebinding 防护，
经 nginx 进来的真实域名不在白名单会返回 421。填 `MEM_MCP_ALLOWED_HOSTS`。

**裸 `/mcp`（无尾斜杠）**会被 Starlette 的 `redirect_slashes` 转成 307，
POST 跟随重定向时会丢 body。本项目用一个纯 ASGI 中间件在路由前改写路径。

### GitHub 备份

```bash
# .env
MEM_GITHUB_REMOTE=git@github.com:you/memorys-data.git   # 共用一仓，按 u<uid> 分支隔离
# 或
MEM_GITHUB_REMOTE=git@github.com:you/memorys-{uid}.git  # 一人一仓
```

定时推送用 systemd timer 而不是 cron —— timer 有 `Persistent=true`，
关机错过的会在开机后补跑，cron 那一次直接丢。

推送脚本走 REST 接口而不是自己拼 `git push`：推送逻辑在 `gitsvc.sync_to_github` 里，
绕过就有两份逻辑要维护。

验证备份真的可用，别只看接口返回 `ok:true`：

```bash
git clone --depth 1 --branch u<uid> git@github.com:you/memorys-data.git /tmp/verify
ls /tmp/verify   # 数一下 md 文件，抽查 frontmatter
```

## 测试

```bash
export PYTHONPATH=.
bash tests/acceptance.sh        # 一键跑全部十三套 + 服务状态 + 端点连通
bash tests/cleanup.sh           # 清测试残留
```

单跑：

```bash
.venv/bin/python tests/smoke_ready.py           # 开箱验收 20 项（只打公网接口）
.venv/bin/python tests/e2e_test.py              # REST 全流程 26 项
.venv/bin/python tests/mcp_test.py              # MCP 工具 14 项
.venv/bin/python tests/branch_test.py           # 分支管理 23 项
.venv/bin/python tests/branch_write_test.py     # 跨分支写入隔离 25 项
.venv/bin/python tests/bootstrap_budget_test.py # token 预算 22 项
.venv/bin/python tests/rank_test.py             # 排序质量 6 项
.venv/bin/python tests/vec_search_test.py       # 向量检索契约 13 项
.venv/bin/python tests/search_unit_test.py      # 分词与扩展 26 项
.venv/bin/python tests/git_push_test.py         # GitHub 备份链路 13 项

.venv/bin/python tests/ab_vector.py             # 两路 vs 三路 A/B（非断言）
.venv/bin/python tests/recall_audit.py          # 召回质量基线（非断言）
.venv/bin/python tests/probe_dashscope.py       # embedding 上游能力探测
```

工具脚本：

```bash
.venv/bin/python tests/mint_key.py              # 建 API Key
.venv/bin/python tests/reindex_vec.py           # 全量重建索引（含向量，慢）
.venv/bin/python tests/reindex_lex_only.py      # 只刷关键词索引，保留向量（快）
```

## 踩过的坑

留在这里因为它们都花过时间，且换个人做还会再踩一次。

### SQLAlchemy / asyncpg

1. `text()` 里写 `:param::text` 会被当成命名参数解析报错 → 用 `CAST(:param AS text)`
2. asyncpg 对 `:lib IS NULL` 推不出类型 → `AmbiguousParameterError`，同样用 `CAST`
3. `Document.tags.cast(str)` 报错 → `func.cast(Document.tags, sqlalchemy.String)`
4. commit 后访问 ORM 属性可能 `MissingGreenlet` → 先 `await session.refresh(doc)`
5. `vector` 扩展要在目标库里单独装，装在 postgres 库里不算

### FastAPI / MCP

6. `mcp.streamable_http_app()` 挂载后必须在 lifespan 里 `async with mcp.session_manager.run()`
7. lifespan 引用的模块必须在文件顶部 import，函数内 import 会在 reload 时出问题
8. **FastAPI 的 `root_path` 会破坏 `app.mount()`** → 自己处理前缀
9. **FastMCP 在 `host=127.0.0.1` 时自动开 DNS rebinding 防护** → 反代域名收到 421
10. **裸 `POST /mcp` 被 `redirect_slashes` 转 307** → 用纯 ASGI 中间件在路由前改写
11. `StaticFiles(directory=...)` 的目录必须先存在，否则启动即崩

### 检索与排序

12. `_tsvector_literal` 的产物是 `to_tsvector` 的输入，不能用 `|` 分隔（那是 tsquery 语法）
13. slug 去重必须算上软删除行，否则恢复时撞车
14. **配上 embedding 会让排序逻辑被整体绕过**（真出过的严重 bug）：
    原来是"先裸调 `semantic_search`，为空才 fallback 到 `search_chunks`"。
    没配 embedding 时永远走 fallback，看不出问题；一配上就变成纯余弦排序，
    绕过 RRF 融合、长度归一化、importance 加权、`PER_DOC_CAP`，
    而且 `library`/`project` 过滤参数根本没往下传。
    **教训**：融合入口只能有一个，别在调用侧留"快捷路径"。
15. **hnsw 索引维度写死在表达式里，换维度不报错而是静默失效** →
    按维度命名索引，启动时删掉维度不符的旧索引；查询侧表达式必须逐字匹配
16. **向量路要单独限每篇文档的 chunk 数**：长文档 chunk 多 → 更多机会挤进向量 top-N
    → 每个 chunk 独立贡献 RRF 分数 → 长文靠"票多"拿回长度归一化刚抵掉的优势
17. **写入侧和查询侧的超时预算必须分开**，且保护要放在融合层用 `asyncio.wait_for` 兜，
    不能只靠 embedding 函数内部 —— 换实现时才不会漏
18. **上游 embedding 会随机超时**：同一条文本单独重发也可能超时，而更长的下一条正常。
    所以要三级降级：整批重试 → 拆单条 → 才认输。实测覆盖率从 0/28 变成 25/28

### bootstrap 预算

19. 装箱循环超额要 `break` 不是 `continue`。`continue` 会让排序失效
    （收的是"能塞进缝隙的"而非"排在前面的"）
20. 别写 `if used + cost > budget and picked: continue` —— `and picked` 短路
    使第一篇永远无条件全量收录，预算 100 也返回 3430 tokens
21. 别把"因单篇长度上限截断"当成"预算耗尽"，要用两个变量区分
22. `digest` 字段也要守预算，它是给 agent 直接塞进开场的

### git 与数据

23. **往别的分支写文件不要用 `checkout` 来回切**：要翻动两次工作区、重建两次索引，
    中途失败会把用户留在错误分支。用 plumbing：
    `hash-object` → 临时 `GIT_INDEX_FILE` + `read-tree`/`update-index` →
    `write-tree` → `commit-tree` → `update-ref`。写完 `git status` 仍为空
24. **写非当前分支时不要落盘也不要进 DB**：磁盘 md 和 PG 索引都是"当前分支"的平铺视图，
    混写会让工作区变成两分支混合体、检索返回不存在的文档
25. 切分支/合并后一定要 reindex
26. 分支名会拼进 git 命令，**必须白名单校验**（`--force`、`a..b`、`x@{1}` 这些都要拒）
27. **软删除时要一并删 chunk**：它们检索不到（查询都带 `deleted_at IS NULL`），
    但会让"多少 chunk 有向量"这类统计失真。chunk 是可重建的派生数据
28. `_reindex_document(do_embed=False)` 会**丢掉已有向量** —— 它是先 DELETE 整行再重插。
    只想刷关键词索引就用 `tests/reindex_lex_only.py`

### 测试

29. 测试断言别依赖失败文案，上游改个措辞就红
30. 测试标题要唯一化（拼时间戳），否则并行跑互相干扰
31. **反向用例不能断言"零结果"**：向量检索的最近邻永远有 N 条，
    「孜然羊肉」也会返回技术文档。判据改成分数分布 —— 无关查询 top1
    要低于正向 top1 的最低分
32. 反向断言别打真实库：库内容会变，「注意」这类通用词会被正常命中（那是对的行为）

### UI

33. **对比度要算不要看**。写脚本取 `computedStyle` 算 WCAG 比值。
    目测会系统性高估细描边和小字号元素，放大截图看更会
34. **别用半透明色承载语义**。alpha 的实际对比度取决于背后是什么，
    在基础表面刚好达标的值换到卡片/hover 态就失效
35. 字号越小颜色要越深。分组标题一度用最浅色配最小字号，方向反了
36. 主题初始化脚本必须放 `<head>`，放 `body` 会先渲染一帧默认主题再跳变

## 技术栈

Python 3.11 / FastAPI / SQLAlchemy(asyncpg) / PostgreSQL + pgvector + pg_trgm /
jieba / MCP SDK（钉 `<2`，2.x 把 `FastMCP` 改名 `MCPServer`）/
单文件 HTML + 原生 JS（无构建步骤）

## License

MIT
