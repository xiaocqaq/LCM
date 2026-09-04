# memorys 本地模式

在自己的机器上跑同一个知识库，不需要 PostgreSQL、不需要域名、不需要建 API Key。

```bash
git clone https://github.com/xiaocqaq/LCM.git memorys && cd memorys
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.local
```

浏览器会自己打开 `http://127.0.0.1:8650/`。数据落在 `~/.memorys/`。

就这样，没有第四步。

---

## 跟服务器模式的区别

| | 服务器模式 | 本地模式 |
|---|---|---|
| 启动 | systemd + nginx 反代 | `python -m app.local` |
| 数据库 | PostgreSQL + pgvector | SQLite（自动建） |
| 登录 | 复用 ai.xlingo.fun 账号 | 免登录，单用户 |
| 监听 | 127.0.0.1:8649 ← nginx | 127.0.0.1:8650 |
| 数据 | `/var/lib/memorys` | `~/.memorys` |
| 必填配置 | `MEM_DATABASE_URL` + `MEM_JWT_SECRET` | 无 |

**业务行为完全一样**：同一套 REST API、同一套 MCP 十工具、同一个 Web UI、
同一套检索排序（RRF 三路融合 + 长度归一化 + importance 加权 + PER_DOC_CAP）。
换句话说，本地模式不是"精简版"，是同一个程序换了个数据库后端。

---

## 检索：一样的结果，不一样的实现

三路检索在两个后端上各有实现，融合与加权只有一份代码：

| | PostgreSQL | SQLite |
|---|---|---|
| 关键词 | `tsvector` + `ts_rank`，GIN 索引 | FTS5 + `bm25()` |
| 模糊兜底 | `pg_trgm.similarity()`，GIN 索引 | Python 端三元组 Jaccard，全扫 |
| 向量 | `pgvector` + hnsw 近邻索引 | Python 端暴力余弦，全扫 |

后两项在 SQLite 上是 O(n) 全表扫描，所以设了扫描上限
（模糊 20000 chunk、向量 50000 chunk），超过就跳过那一路而不是硬扫 ——
检索是交互路径，宁可少一路召回也不能突然卡住。

`GET /api/v1/system` 会把当前实际走的实现报出来：

```json
{
  "mode": "local",
  "search": {
    "backend": "sqlite",
    "keyword": "fts5+bm25",
    "fuzzy": "python-trigram(全扫，上限 20000 chunk)",
    "vector_index": "python-bruteforce(全扫，上限 50000 chunk)",
    "chunk_count": 250
  }
}
```

### 两端结果一致性是实测过的，不是推断的

`tests/local_parity.py` 做两层验证：

**第一层**：`pg_trgm.similarity()` 的 Python 复刻，拿真实 PG 逐条对照数值。

```
similarity('部署流程', '部署')                 PG 0.333333  Python 0.333333
similarity('postgres 连接串', '连接串 postgres') PG 1.000000  Python 1.000000
similarity('向量检索接入', '向量检索')          PG 0.500000  Python 0.500000
similarity('fsdp-service 工程结构', 'fsdp service 结构')  PG 0.700000  Python 0.700000
```

有个反直觉的发现：对中文，`show_trgm('部署流程')` 在 PG 里返回的是
`{0x78cd9f, 0x8b57cf, …}` 十六进制串，不是可读的 `'  部'`、`' 部署'` ——
pg_trgm 对多字节字符先做 CRC32 再取低位，避免变长编码把窗口切在字节中间。
所以两边的**三元组形式不同，但相似度数值完全相同**（哈希是双射，Jaccard 不变）。
不要试图让 Python 侧输出跟 `show_trgm` 一样的字符串，那既做不到也没必要。

**第二层**：同一批文档写进两个后端，同一批查询对比 top1 和命中集合。
实测 5 组查询 **top1 一致率 100%**。

标准刻意不是"结果字节级相同"：bm25 和 ts_rank 是不同的打分函数，排名细节必然有差异。
要求的是"用户问同一个问题，两边都能找到那篇文档"。

---

## 向量检索（可选）

不配也能用 —— 关键词 + 模糊两路覆盖大部分场景。想开第三路，在 `~/.memorys/.env` 里写：

```ini
MEM_EMBED_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
MEM_EMBED_API_KEY=sk-xxx
MEM_EMBED_MODEL=text-embedding-v4
MEM_EMBED_DIM=1024
```

`MEM_EMBED_DIM` 必须跟上游实际返回的维度一致。填错不会报错，而是索引静默失效
（写入时维度不符的向量被剔掉，检索时那一路永远返回空）—— 这个坑在服务器上踩过。

配好后 `POST /api/v1/sync {"action":"reindex"}` 给已有文档补向量。

---

## 挂到 AI agent

本地模式的 MCP 地址是 `http://127.0.0.1:8650/mcp`，**免鉴权时不需要任何请求头**。

Claude Desktop / Cursor / VS Code（JSON）：

```json
{
  "mcpServers": {
    "memorys": {
      "type": "http",
      "url": "http://127.0.0.1:8650/mcp"
    }
  }
}
```

Codex CLI（TOML，`~/.codex/config.toml`）：

```toml
[mcp_servers.memorys]
url = "http://127.0.0.1:8650/mcp"
```

注意这是远程 HTTP 服务，**不要写 `command`** —— 写了会被当成"启动本地进程"，
报 `program path has no file name`。

---

## 命令行参数

```bash
python -m app.local --port 9000          # 换端口（被占用会自动往后找）
python -m app.local --data ~/mem-work    # 换数据目录，多套库互不干扰
python -m app.local --no-browser         # 不自动开浏览器
python -m app.local --reload             # 改代码自动重启（开发用）
```

---

## 安全边界

本地模式默认免鉴权（`MEM_LOCAL_OPEN=true`），意味着**任何能连上这个端口的进程
都是那个单用户**。所以：

- 绑 `127.0.0.1` → 随便用
- 绑其他地址 → **拒绝启动**

```
$ python -m app.local --host 0.0.0.0
启动中止：--host 0.0.0.0 会让服务监听非本机地址，而本地模式默认是免鉴权的。
任何能连到这个端口的人都会被当成用户 local，能读写你全部的记忆。
```

这个检查做成"拒绝启动"而不是"打个警告"，因为警告会被无视，
而一个免鉴权的知识库暴露在局域网上，泄露的是全部项目记忆。

要暴露到局域网，两步：设 `MEM_LOCAL_OPEN=false`，然后在 Web UI 的
「API Key」页建一个 Key，客户端带 `X-Api-Key` 访问。

### 本地模式不读项目根的 `.env`

这是安全边界，不是风格选择。服务器部署时项目根的 `.env` 里是生产 PG 连接串和
生产 JWT secret。如果本地模式也读它，那么在服务器上 clone 了代码、
或者把 `.env` 带到本地的人，一执行 `python -m app.local` 就**直接连上了生产库** ——
本地随手测试的增删改会落到真实数据上，而界面上完全看不出区别。

所以本地模式只读 `~/.memorys/.env`。想在本地连生产库排查问题，
显式传 `MEM_DATABASE_URL=...` 环境变量 —— 那是有意识的动作。

---

## 数据在哪、怎么备份

```
~/.memorys/
├── memorys.db          SQLite（索引 + 元数据，可重建）
├── jwt_secret          自动生成，600 权限
├── .env                你自己的配置覆盖（可选）
└── data/data/users/u1/
    ├── .git/           每次写入自动 commit
    └── main/*.md       ← 真正的数据在这里
```

**md 文件是 source of truth**，数据库只是索引。所以备份只需要备份 `data/` 目录
（或者直接推 git 远端）。数据库删了不丢数据：

```bash
rm ~/.memorys/memorys.db
python -m app.local          # 重建空库
curl -X POST http://127.0.0.1:8650/api/v1/sync -d '{"action":"reindex"}' \
     -H 'Content-Type: application/json'    # 从 md 重建索引
```

想推 GitHub 长期备份，在 `~/.memorys/.env` 里配 `MEM_GITHUB_REMOTE`，
然后 Web UI 的「同步 / GitHub」页点「立即推送」。
本地模式没有 systemd timer，定时推送要自己挂 cron。

---

## SQLite 特有的坑（都已修，记下来免得复发）

**1. `BIGINT PRIMARY KEY` 不会自增**

SQLite 里只有 `INTEGER PRIMARY KEY` 才是 rowid 的别名并自动分配值。
`chunks.id` 原来声明成 `BigInteger`，在 SQLite 上插入直接
`NOT NULL constraint failed: chunks.id` —— 而报错完全没提类型不对，
看着像是代码忘了传 id。修法是 `BigInteger().with_variant(Integer(), "sqlite")`。

**2. FTS5 没有外键级联**

关键词索引是独立的虚拟表 `chunks_fts`，删 chunk 不会带走它的 FTS 行。
漏了显式删除的后果：检索时 JOIN 不上就静默少结果，而 `count(*)` 又对不上，很难查。
而且删除顺序有讲究 —— 必须**先删 FTS 行再删 chunk**，因为 FTS 行是按
`chunk_id IN (SELECT id FROM chunks WHERE …)` 反查的，chunk 先没了那个子查询就返回空集。

**3. PRAGMA 是连接级的，不是数据库级**

`foreign_keys` 默认**关闭**，而模型依赖 `ondelete="CASCADE"`。
在 lifespan 里执行一次 PRAGMA 只对那一条连接有效，后续从池里拿到的新连接又变回 OFF ——
于是删文档不级联删 chunk，留下孤儿行，而且只在"连接池扩容之后"才出现，极难复现。
正确做法是挂 `event.listens_for(engine, "connect")`，每条新连接都重设。

**4. `ADD COLUMN IF NOT EXISTS` 不支持**

老库升级要先读 `PRAGMA table_info(表名)` 再决定加不加列。补列成功会打日志 ——
静默补列出问题时无从排查。

**5. `embedding::text` 转换在 SQLite 上会报错**

PG 上那一列是 `vector(dim)` 类型，读回文本要 `::text`；SQLite 上它本来就是 TEXT，
加转换直接语法错误。

---

## 什么时候该换回 PostgreSQL

本地模式的模糊路和向量路都是全表扫描。手上这个库（33 篇 / 250 chunk）是毫秒级，
到两万 chunk 以上会明显变慢。`/api/v1/system` 的 `chunk_count` 接近扫描上限时，
那两路会直接跳过（检索仍可用，只是少两路召回）。

判断标准很简单：`chunk_count` 上万且觉得搜得慢了，就该上 PG 了。
