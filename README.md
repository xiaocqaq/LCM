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

### 本地跑（零配置）

```bash
git clone https://github.com/xiaocqaq/LCM.git memorys && cd memorys
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.local
```

浏览器自动打开 `http://127.0.0.1:8650/`，数据落在 `~/.memorys/`。
不需要 PostgreSQL、不需要填任何配置、不需要建 API Key。
SQLite 自动建，JWT secret 自动生成并持久化，MCP 免鉴权直连。

详见 [docs/LOCAL.md](docs/LOCAL.md) —— 包括两端检索一致性的实测数据、
SQLite 特有的坑、以及什么时候该换回 PostgreSQL。

### 服务器部署

```bash
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

### 两种模式的关系

**同一个程序，同一套业务代码**，不是两个分支也不是精简版：

| | 服务器模式 | 本地模式 |
|---|---|---|
| 数据库 | PostgreSQL + pgvector | SQLite（自动建） |
| 关键词检索 | `tsvector` + `ts_rank` | FTS5 + `bm25()` |
| 模糊兜底 | `pg_trgm`，GIN 索引 | Python 三元组 Jaccard |
| 向量 | pgvector + hnsw 索引 | Python 暴力余弦 |
| 登录 | 复用上游账号体系 | 免登录单用户 |
| 必填配置 | 2 项 | 0 项 |

方言差异全部收在 `app/dialect.py`，**检索的融合与加权只有一份实现** ——
那部分逻辑（RRF 三路等权、长度归一化指数 0.35、importance ±10%、PER_DOC_CAP=2）
是被实测反复校准出来的，复制一份必然分叉。

一致性是实测过的：`tests/local_parity.py` 拿真实 PG 的 `similarity()` 逐条对照
Python 复刻的数值（6 组全部吻合到 1e-6），再用同一批文档和查询对比两端结果，
**top1 一致率 100%**。

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

### 增量 reindex：向量按内容复用

`_reindex_document` 先把旧 chunk 的「正文 → 向量」映射取出来，
只给**内容真的变了**的 chunk 调 embedding 上游。

这不是优化而是必须：一次全量 reindex 要给每个 chunk 调一次上游，
而超限的 chunk 会挂死到 ReadTimeout（见下节）。实测 23 篇文档全量 reindex 超过 7 分钟，
直接把 `POST /api/v1/sync` 打成超时。改成复用后同样的 sync 是 **0.18 秒**。

### 上游 embedding 的 token 上限：不是"随机超时"

一度以为阿里云百炼会**随机** ReadTimeout（跟内容、长度、批量都无关）。
后来做了二分定位，发现它完全是确定性的 —— `qwen3.7-text-embedding-flash`
的上限是 **512 token**，超限后**不返回 400 而是挂住到 ReadTimeout**：

```
1015 字 → HTTP 200,  usage.total_tokens=513, 221ms
1019 字 → HTTP 200,  usage.total_tokens=503, 244ms
1060 字 → ReadTimeout（同一条重试 3 次全超时；截短 20 字立刻 200）
```

之所以看着"随机"，是因为 chunk 长度分布跨过了这条线，而重试策略把每条超限的
chunk 放大成 27 次超时（整批 ×2 + 拆开单条 ×25）。所以修法不是加重试，
而是**按 token 预算截断**（`fit_tokens()`）。

token 估算器必须**宁高估勿低估**，系数是拿真实 `usage.total_tokens` 校准的
（`tests/calib_tokens.py`，12 条真实 chunk 逐条对照）：

| 版本 | 系数 | 12 条实测估/实比值 | 结果 |
|---|---|---|---|
| 第一版 | CJK 1/字，其余 4 字符/token | **0.78 – 1.08** | 10 条低估 ❌ |
| 现在 | CJK ×1.05，其余 ÷2.2 | 1.11 – 1.51 | 全部安全 ✅ |

第一版低估的原因：非中文部分大量是代码、路径、YAML、命令行，标点密集、
BPE 切得很碎，实测只有 2.4-2.9 字符/token，远不到 4。

修完的效果：23 条历来拿不到向量的长 chunk **23/23 全部成功**，
向量覆盖率 `92.3% → 100%`，补全部耗时 6.2 秒。
`text-embedding-v4` 可以放宽到 8192 token（实测 8000 字正常返回）。

补历史遗留的缺失向量用 `tests/backfill_vectors.py`（sync 只处理内容变过的文档，
内容没变但缺向量的它不管 —— 那是刻意的，否则每次 sync 都要为历史失败重试一遍）。

复用的键是 chunk 正文本身，**不是 seq** —— 在文档中间加一节会让后面所有 chunk 的
seq 整体位移，按 seq 复用等于把向量和内容错配，检索会返回莫名其妙的结果。

只想让改过的同义词表生效、不想重算向量时，用 `tests/reindex_lex_only.py`
（只 UPDATE tsv 列，不动 chunk 行）。

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
bash tests/acceptance.sh        # 一键跑全部十四套 + 服务状态 + 端点连通
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
.venv/bin/python tests/links_test.py           # 文档关系 30 项（supersedes/implements/撤销）
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

50 条，留在这里因为它们都花过时间，且换个人做还会再踩一次。

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
18. **上游 embedding 的"随机超时"其实是确定性的 token 上限**：
    flash 模型 512 token，超限后不返回 400 而是挂死到 ReadTimeout。
    看着随机是因为 chunk 长度跨过那条线，而重试策略把每条超限 chunk 放大成 27 次超时。
    修法是按 token 预算截断，不是加重试。三级降级保留，用于真正的网络抖动
19. **token 估算器必须宁高估勿低估**，且系数要拿真实 `usage.total_tokens` 校准。
    "非中文 4 字符/token" 那个常识值在代码/路径/YAML 密集的文本上不成立
    （实测 2.4-2.9），照抄会低估 22%，直接导致请求挂死

### bootstrap 预算

20. 装箱循环超额要 `break` 不是 `continue`。`continue` 会让排序失效
    （收的是"能塞进缝隙的"而非"排在前面的"）
21. 别写 `if used + cost > budget and picked: continue` —— `and picked` 短路
    使第一篇永远无条件全量收录，预算 100 也返回 3430 tokens
22. 别把"因单篇长度上限截断"当成"预算耗尽"，要用两个变量区分
23. `digest` 字段也要守预算，它是给 agent 直接塞进开场的

### git 与数据

24. **往别的分支写文件不要用 `checkout` 来回切**：要翻动两次工作区、重建两次索引，
    中途失败会把用户留在错误分支。用 plumbing：
    `hash-object` → 临时 `GIT_INDEX_FILE` + `read-tree`/`update-index` →
    `write-tree` → `commit-tree` → `update-ref`。写完 `git status` 仍为空
25. **写非当前分支时不要落盘也不要进 DB**：磁盘 md 和 PG 索引都是"当前分支"的平铺视图，
    混写会让工作区变成两分支混合体、检索返回不存在的文档
26. 切分支/合并后一定要 reindex
27. 分支名会拼进 git 命令，**必须白名单校验**（`--force`、`a..b`、`x@{1}` 这些都要拒）
28. **软删除时要一并删 chunk**：它们检索不到（查询都带 `deleted_at IS NULL`），
    但会让"多少 chunk 有向量"这类统计失真。chunk 是可重建的派生数据
29. `_reindex_document(do_embed=False)` 会**丢掉已有向量** —— 它是先 DELETE 整行再重插。
    只想刷关键词索引就用 `tests/reindex_lex_only.py`
30. **读路径里的写操作要用独立 session**。给检索命中的文档累加 `access_count` 时
    挂在调用方 session 上，UPDATE 会随请求结束被丢掉 —— FastAPI 的 `get_session`
    从不 commit。而且不该为了记账把检索变成写事务（调用方可能在更大的只读逻辑里）。
    记账失败也绝不能影响检索本身，整段包 try。
31. **关系必须能撤销，否则会造成永久隐身**。`supersedes` 让目标退出检索，
    那么：改 links 要先清旧标记再重建；删除"取代者"要恢复被取代者；
    从磁盘 sync 要整体重建而不是增量改。漏任何一条，都会出现
    "某篇文档搜不到了但看不出为什么"。
32. **sync 时 links 要在全部文档进 DB 之后再统一解析**。关系可能指向本次 sync
    里更靠后才扫到的文件，边扫边解析会有一半解析不出来。
33. **启动期 DDL 必须设 `lock_timeout`**（本项目最难查的一次故障）。
    `ALTER TABLE` 要 ACCESS EXCLUSIVE 锁，只要有一个连接还占着该表的锁
    （比如一个 `idle in transaction` 的孤儿连接），ALTER 就排队等待 ——
    而 **PG 的锁队列是 FIFO：一个待授予的 ACCESS EXCLUSIVE 会挡住它后面
    所有对该表的读写**。结果整张 `chunks` 表连 SELECT 都做不了，
    检索/reindex/sync 全部挂死，表面症状只是"请求超时"，
    完全看不出跟启动期 DDL 有关。
    诊断方法：`pg_stat_activity` 看 `wait_event_type='Lock'`，
    再用 `pg_blocking_pids()` 找源头。
    修法：`SET lock_timeout = '3s'` + 幂等 DDL 先查状态再改
    （`attstorage` 已是 `e` 就别再 ALTER）。
34. **asyncpg 把 `"char"` 类型返回成 bytes**。`pg_attribute.attstorage` 拿到的是
    `b'e'` 而不是 `'e'`，直接和字符串比永远不等 → "先查再改"的优化失效，
    每次启动照样抢一次表锁。
35. **诊断脚本一定要 `python -u` 或 `print(flush=True)`**。前几轮排查这个死锁时
    脚本卡在 PG 等锁上，stdout 缓冲导致日志一直是空的，
    看起来像"脚本自己挂了"，白绕了两圈。

### 测试

36. 测试断言别依赖失败文案，上游改个措辞就红
37. 测试标题要唯一化（拼时间戳），否则并行跑互相干扰
38. **反向用例不能断言"零结果"**：向量检索的最近邻永远有 N 条，
    「孜然羊肉」也会返回技术文档。判据改成分数分布 —— 无关查询 top1
    要低于正向 top1 的最低分
39. 反向断言别打真实库：库内容会变，「注意」这类通用词会被正常命中（那是对的行为）

### UI

40. **对比度要算不要看**。写脚本取 `computedStyle` 算 WCAG 比值。
    目测会系统性高估细描边和小字号元素，放大截图看更会
41. **别用半透明色承载语义**。alpha 的实际对比度取决于背后是什么，
    在基础表面刚好达标的值换到卡片/hover 态就失效
42. 字号越小颜色要越深。分组标题一度用最浅色配最小字号，方向反了
43. 主题初始化脚本必须放 `<head>`，放 `body` 会先渲染一帧默认主题再跳变

### SQLite（本地模式）

44. **`BIGINT PRIMARY KEY` 在 SQLite 上不自增**。只有 `INTEGER PRIMARY KEY`
    是 rowid 的别名。`chunks.id` 声明成 BigInteger 时插入直接
    `NOT NULL constraint failed: chunks.id` —— 报错完全没提类型不对，
    看着像代码忘了传 id。修法 `BigInteger().with_variant(Integer(), "sqlite")`
45. **FTS5 虚拟表没有外键级联**。删 chunk 不会带走它的 FTS 行，
    漏了显式删除会让检索 JOIN 不上而静默少结果，`count(*)` 又对不上。
    而且**必须先删 FTS 行再删 chunk** —— FTS 行是按
    `chunk_id IN (SELECT id FROM chunks WHERE …)` 反查的，chunk 先没了子查询就空
46. **PRAGMA 是连接级不是数据库级**。`foreign_keys` 默认关闭，而模型依赖
    `ondelete="CASCADE"`。在 lifespan 里设一次只对那条连接有效，
    池扩容后拿到的新连接又是 OFF —— 孤儿行只在"连接池扩容之后"出现，极难复现。
    要挂 `event.listens_for(engine.sync_engine, "connect")`
47. **`with_variant` 的基类型必须是通用的那个**。写成
    `JSONB().with_variant(JSON, "sqlite")` 在 SQLite 上仍会编译 JSONB 而报错 ——
    variant 只在命中的方言上替换，没命中时用基类型
48. **本地模式不能读项目根的 `.env`**。那里面是生产 PG 连接串和生产 secret，
    读了就等于本地随手测试直接改生产数据，而界面上看不出区别。
    只读 `~/.memorys/.env`
49. **`embedding::text` 转换在 SQLite 上报错**。PG 上那列是 `vector(dim)`，
    读回文本要 `::text`；SQLite 上本来就是 TEXT，加转换是语法错误
50. **单用户模式要自己建 git repo**。server 模式是在登录时建的，本地不走登录 ——
    漏了这步后果很隐蔽：md 确实落盘、写入一切正常，但 `try_commit_all` 内部
    catch 掉所有异常，git 提交静默不发生，版本历史永远是空的

## 技术栈

Python 3.11 / FastAPI / SQLAlchemy(asyncio) / MCP SDK（钉 `<2`，2.x 把
`FastMCP` 改名 `MCPServer`）/ jieba / 单文件 HTML + 原生 JS（无构建步骤）

存储层两套后端，同一套 ORM 模型（列类型走 `app/coltypes.py` 的方言变体）：

- **服务器**：PostgreSQL + asyncpg + pgvector + pg_trgm
- **本地**：SQLite + aiosqlite + FTS5（`app/dialect.py` 里的 Python 侧模糊/向量实现）

## License

MIT

---

## 文档关系（links）

参考 [graph-memory](https://github.com/adoresever/graph-memory) 的边模型加进来的，
但只保留三种，且每种都必须**实际改变检索行为** —— 一条边如果不影响
"该给 agent 看什么"，它就只是装饰。

| type | 效果 |
|---|---|
| `supersedes` | 目标**从检索和 bootstrap 里退场**（文件仍在，仍可 `memory_get` 读） |
| `implements` | 命中任一篇时把另一篇一起带进 bootstrap，标注 `via="implements"` |
| `relates` | 仅作记录，不改检索 |

写法（frontmatter 或 API 都行）：

```yaml
---
title: 部署流程 v2
type: howto
links:
  - type: supersedes
    target: 部署流程 v1        # 标题、slug 或 "#123" 都能解析
    note: v1 的手工 scp 已废弃
---
```

手写 md 时也支持简写：`links: ["supersedes:部署流程 v1"]`。

### 为什么需要这个

实测库里有 **3 篇讲同一个项目工程结构的文档**（标题相似度 0.47-0.60，
小节标题几乎一一对应），是同一份知识写了三遍。检索时三篇全命中，
agent 无从判断该信哪个 —— 那比没有信息更糟。

`supersedes` 不是删除：旧版本仍在磁盘、仍在 git 历史、仍能直接读，
只是不再参与检索。「过时」和「不存在」是两件事。

### 为什么不照搬整套图谱

graph-memory 把知识拆成 `TASK/SKILL/EVENT` 三类节点 + 五类边，
靠 LLM 从对话里自动抽取，再跑 Label Propagation 社区检测和 Personalized PageRank。
那套东西解决的是**"对话流水如何变成结构化知识"**。

memorys 的输入不是对话流水 —— 是 agent 或人**已经想清楚了才写下**的成篇记忆。
所以：

- **不需要抽取层**。写入时结构已经有了（type/project/tags/importance），
  再上一层 LLM 抽取是把已有结构拆碎重组，纯损耗。
- **不需要社区检测**。23 篇文档跑 Label Propagation 是自娱自乐；
  `project` 字段已经是人工划好的社区，比算出来的准。
- **不需要 PageRank**。图里只有几十条边，PPR 的收益来自图的连通密度，
  这个规模下等于按边数排序。

真正值得借的只有两个思路，都对应实测看到的具体问题：

2. **版本关系**（对应它的 `PATCHES` 边 + 向量去重）→ `supersedes`
3. **客观累积的重要度**（对应它的 `validatedCount`）→ `access_count`

### access_count：手填 importance 的补充

实测 **96% 的文档手填 `importance ≥ 4`** —— 人在写的时候都觉得自己写的重要，
这个字段已经没有区分度了。

graph-memory 用 `validatedCount`（节点被重复提取到就 +1）解决同一个问题。
关键在于那是**客观累积**的信号，不是写入时的自我评价。

这里用 `access_count`：真正进了检索结果集的文档才 +1。

```
有效重要度 = importance + min(1.0, access_count / 10)
```

权重刻意压到最多顶 1 级，因为热度只是"被读过"，不等于"重要" ——
一篇写错的文档也可能被反复检索到。

**热度只用在 bootstrap 的取舍上，不进检索打分**。检索里加热度会形成正反馈：
排前面 → 被读到 → 排更前面，最后热门文档垄断所有查询。

### 关系是软状态，随时可撤

- 改 `links` 会先清掉旧的 supersede 标记再重建 —— 否则改了目标之后，
  旧目标永久隐身且没有任何地方能看出原因
- 删除一篇"取代者"时，被它取代的文档自动恢复可见
- `sync_from_disk` 会**整体重建**所有关系而不是增量改：md 是 source of truth，
  磁盘上没有的关系不该在 DB 里留着
- 解析不出来的 target 会在 `linkReport.unresolved` 里报出来，不静默失败 ——
  用户以为 supersedes 生效了、旧文档其实还在检索里，这种失败最难查

查关系全貌：`GET /api/v1/documents/{id}/related`（出边 + 入边）。
