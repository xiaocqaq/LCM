"""数据库方言适配：同一套业务代码同时跑 PostgreSQL（服务器）和 SQLite（本地）。

## 为什么不做成两份代码

这个项目最值钱也最容易出错的部分是检索排序 —— RRF 三路融合、长度归一化、
importance 加权、PER_DOC_CAP、access_count 记账。这些逻辑是被实测反复校准出来的
（见 README「检索排序」节），复制一份必然分叉，改一边忘一边。

所以这里只抽"数据库能力"，排序逻辑在 service.py 保持单一实现。
两端返回同样形状的候选集 `(chunk_id, doc_id, seq, heading, content, rank)`，
后面的融合与加权完全共用。

## 两端能力差异（这是本模块存在的全部原因）

| 环节 | PostgreSQL | SQLite |
|---|---|---|
| 关键词 | `tsvector` + `ts_rank`，GIN 索引 | FTS5 + `bm25()` |
| 模糊兜底 | `pg_trgm.similarity()`，GIN 索引 | Python 端字符三元组 Jaccard，全扫 |
| 向量 | `pgvector` + hnsw 近邻索引 | Python 端暴力余弦，全扫 |
| JSON | `JSONB` | `JSON`（存成 TEXT） |

后两项在 SQLite 侧是 O(n) 全表扫描。实测量级见 `local_bench` 测试：
本地库常见规模（几百到几千 chunk）下是毫秒级，两万 chunk 以上会明显变慢 ——
到那个量级就该上 PG 了，本模块会在启动日志里提醒。

## 一个刻意的不对称

SQLite 的 FTS5 用 `bm25()` 打分，而 bm25 自带文档长度归一化；PG 的 `ts_rank` 没有，
所以 service.py 里额外补了一层长度衰减。两边都保留那层衰减是有意的：
它作用在**文档**长度上（跨 chunk），而 bm25 归一化的是**单个 FTS 行**的长度，
量纲不同，不重复。实测两端 top1 一致率见 local_parity 测试。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------- 后端识别

PG = "postgresql"
SQLITE = "sqlite"


def backend_of(url: str) -> str:
    """从连接串判定后端。只认这两种，别的直接报错而不是猜。"""
    u = (url or "").lower()
    if u.startswith(("postgresql", "postgres")):
        return PG
    if u.startswith("sqlite"):
        return SQLITE
    raise ValueError(
        f"不支持的 MEM_DATABASE_URL：{url!r}\n"
        f"服务器模式用 postgresql+asyncpg://…，本地模式用 sqlite+aiosqlite:///…"
    )


# ---------------------------------------------------------------- DDL

# PG 侧的启动期 DDL。原来散在 main.py 的 lifespan 里，挪过来是因为
# 本地模式要走完全不同的一套，混在一个函数里全是 if。
PG_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin(tsv)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_trgm ON chunks USING gin(content gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_documents_user_project "
    "ON documents(user_id, project) WHERE deleted_at IS NULL",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS links JSONB DEFAULT '[]'::jsonb",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS superseded_by INTEGER",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS access_count INTEGER DEFAULT 0",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS idx_documents_not_superseded "
    "ON documents(user_id) WHERE deleted_at IS NULL AND superseded_by IS NULL",
]

# SQLite 侧。
#
# 三点跟 PG 不同，都是踩过的：
# 1. `ADD COLUMN IF NOT EXISTS` SQLite 不支持 → 得先查 pragma 再决定加不加。
#    放在 sqlite_add_missing_columns() 里做。
# 2. WAL 必开：默认的 journal 模式下，写事务会阻塞读事务。本地模式虽然单用户，
#    但 Web UI 轮询 + MCP 客户端 + 后台 reindex 是三个并发读者。
# 3. foreign_keys 默认是**关的**。models.py 依赖 ondelete="CASCADE"
#    （删 document 要连带删 chunks），不开就会留下孤儿行 —— 这个坑在 PG 上
#    永远不会遇到，因为 PG 的外键始终生效。
SQLITE_PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",   # WAL 下 NORMAL 已经够安全，FULL 每次提交都 fsync
    "PRAGMA busy_timeout=5000",    # 撞锁等 5 秒再报错，而不是立刻 database is locked
]

SQLITE_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_documents_user_project ON documents(user_id, project)",
    "CREATE INDEX IF NOT EXISTS idx_documents_not_superseded ON documents(user_id, superseded_by)",
    # FTS5 关键词索引。
    #
    # 存的是 expand_tokens() 产出的**扩展词串**，不是原文 —— 跟 PG 侧
    # to_tsvector('simple', lex) 喂的东西完全一样，这样两端召回集才可比。
    # tokenize=unicode61 + 空格分隔：词已经由 jieba 切好，不需要 FTS5 再切中文
    # （它切不了，unicode61 会把整句连续汉字当成一个 token）。
    #
    # 这里刻意不用 external content 表：external content 要求内容表和 FTS 表
    # 严格同步，一旦 reindex 中途失败就会出现 "database disk image is malformed"
    # 级别的不一致。独立表 + 显式 delete/insert 慢一点但可恢复。
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
    "  lex, chunk_id UNINDEXED, tokenize='unicode61 remove_diacritics 0')",
]

SQLITE_ADD_COLUMNS = {
    "documents": [
        ("links", "TEXT DEFAULT '[]'"),
        ("superseded_by", "INTEGER"),
        ("access_count", "INTEGER DEFAULT 0"),
        ("last_accessed_at", "TIMESTAMP"),
    ],
}


async def sqlite_add_missing_columns(conn) -> list[str]:
    """SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先读 pragma 再补。

    返回实际补了哪些列，给启动日志用 —— 静默补列出问题时无从排查。
    """
    added = []
    for tbl, cols in SQLITE_ADD_COLUMNS.items():
        try:
            rows = (await conn.execute(text(f"PRAGMA table_info({tbl})"))).all()
        except Exception:
            continue
        have = {r[1] for r in rows}
        for name, decl in cols:
            if name not in have:
                try:
                    await conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {name} {decl}"))
                    added.append(f"{tbl}.{name}")
                except Exception:
                    pass
    return added


# ---------------------------------------------------------------- 关键词检索

_FTS_SAFE = re.compile(r"[^0-9a-z\u4e00-\u9fff]")


def fts5_match_expr(tokens: list[str]) -> str:
    """把 jieba 词表拼成 FTS5 MATCH 表达式。

    对应 PG 侧的 `to_tsquery('simple', 'a | b | c')` —— OR 语义，任一词命中即可。
    这是刻意的：AND 语义下"memorys 怎么部署"要求三个词全中，
    而"怎么"已经被停用词表滤掉了，剩下两个词全中的 chunk 可能一个都没有。

    每个词都过一遍白名单再加双引号：FTS5 的查询语法里 `-` 是 NOT、`*` 是前缀、
    `:` 是列限定，jieba 切出来的词里带这些字符会让整条 MATCH 语义变掉甚至报错。
    """
    safe = []
    for t in tokens:
        t = _FTS_SAFE.sub("", t.lower())
        if len(t) > 1:
            safe.append(f'"{t}"')
    return " OR ".join(safe)


async def keyword_candidates(
    session: AsyncSession, backend: str, tokens: list[str], raw_query: str,
    where_sql: str, params: dict, limit: int,
) -> list[tuple]:
    """关键词路候选集。两端都返回 (chunk_id, doc_id, seq, heading, content, rank)。

    where_sql 是已经拼好的文档过滤条件（user/library/project/deleted/superseded），
    由调用方按后端无关的方式给出 —— 那部分 SQL 两端语法一致，不需要适配。
    """
    if not tokens:
        return []

    if backend == PG:
        tsq = " | ".join(tokens)
        sql = text(f"""
            SELECT c.id, c.document_id, c.seq, c.heading, c.content,
                   ts_rank(c.tsv, to_tsquery('simple', :tsq)) AS rank
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {where_sql} AND c.tsv @@ to_tsquery('simple', :tsq)
            ORDER BY rank DESC LIMIT :lim
        """)
        return (await session.execute(sql, {**params, "tsq": tsq, "lim": limit})).all()

    expr = fts5_match_expr(tokens)
    if not expr:
        return []
    # bm25() 返回负数，越负越相关 → 取负号变成"越大越相关"，跟 ts_rank 对齐。
    # 这一步不能省：service.py 的 RRF 只按顺序取 rank，但长度归一化那段
    # 会拿 rank 做乘法，符号反了会把最相关的排到最后。
    sql = text(f"""
        SELECT c.id, c.document_id, c.seq, c.heading, c.content,
               -bm25(chunks_fts) AS rank
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.chunk_id
        JOIN documents d ON d.id = c.document_id
        WHERE chunks_fts MATCH :expr AND {where_sql}
        ORDER BY rank DESC LIMIT :lim
    """)
    return (await session.execute(sql, {**params, "expr": expr, "lim": limit})).all()


# ---------------------------------------------------------------- 模糊兜底

def _trigrams(s: str) -> set[str]:
    """复刻 pg_trgm 的三元组切法。

    PG 的 show_trgm() 行为（实测对照 `SELECT show_trgm('abc')`）：
      1. 非字母数字字符当分隔符，切成词
      2. 每个词前补两个空格、后补一个空格
      3. 滑窗取长度 3 的子串
    所以 'abc' → {'  a', ' ab', 'abc', 'bc '}，单字 'a' → {'  a', ' a '}。

    为什么要逐字复刻而不是随便实现一个：本地模式和服务器模式的召回集必须可比，
    否则同一个查询在两端给出不同结果，用户会以为数据没同步。

    ## 一个实测发现：CJK 的三元组在 PG 里是哈希过的

    对中文，`show_trgm('部署流程')` 返回的是 `{0x78cd9f, 0x8b57cf, …}` 这样的
    十六进制串，不是可读的 '  部'、' 部署'。原因是 pg_trgm 对多字节字符
    先做 CRC32 再取低位，避免变长编码把窗口切在字节中间。

    所以这个函数的**输出形式**跟 PG 不同，但**相似度数值完全一致** ——
    实测 4 组中英混排：
        '部署流程' vs '部署'              PG 0.333333  这里 0.333333
        'postgres 连接串' vs 反序          PG 1.000000  这里 1.000000
        'abc' vs 'abd'                     PG 0.333333  这里 0.333333
        '向量检索接入' vs '向量检索'       PG 0.500000  这里 0.500000
    因为哈希是双射的（同一个三元组恒等映射到同一个值），Jaccard 系数不变。
    验证脚本在 tests/local_parity.py，用真实 PG 连接逐条对照。

    结论：不要试图让这个函数输出跟 show_trgm 一样的字符串，那既做不到也没必要。
    要比的是 similarity() 的数值。
    """
    out: set[str] = set()
    for word in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", (s or "").lower()):
        if not word:
            continue
        padded = "  " + word + " "
        for i in range(len(padded) - 2):
            out.add(padded[i:i + 3])
    return out


def trgm_similarity(a: str, b: str) -> float:
    """pg_trgm.similarity() 的 Python 实现：三元组集合的 Jaccard 系数。"""
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    return inter / len(ta | tb)


# SQLite 侧模糊兜底的扫描上限。
#
# 超过这个数就跳过模糊路而不是硬扫：模糊兜底是"锦上添花"的第二路
# （抗错字/部分词），而检索是交互路径。宁可少一路召回，
# 也不能让本地模式在库变大之后突然卡住 —— 那种退化最难归因。
SQLITE_TRGM_SCAN_CAP = 20000


async def trgm_candidates(
    session: AsyncSession, backend: str, raw_query: str,
    where_sql: str, params: dict, limit: int, threshold: float = 0.2,
) -> list[tuple]:
    """模糊路候选集。"""
    if backend == PG:
        sql = text(f"""
            SELECT c.id, c.document_id, c.seq, c.heading, c.content,
                   similarity(c.content, :q) AS rank
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {where_sql} AND similarity(c.content, :q) > :thr
            ORDER BY rank DESC LIMIT :lim
        """)
        return (await session.execute(
            sql, {**params, "q": raw_query[:2000], "thr": threshold, "lim": limit})).all()

    # SQLite：先数一下规模，超上限直接放弃这一路
    n = (await session.execute(text(
        f"SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        f"WHERE {where_sql}"), params)).scalar() or 0
    if n > SQLITE_TRGM_SCAN_CAP:
        return []

    rows = (await session.execute(text(
        f"SELECT c.id, c.document_id, c.seq, c.heading, c.content "
        f"FROM chunks c JOIN documents d ON d.id = c.document_id WHERE {where_sql}"),
        params)).all()
    q = raw_query[:2000]
    scored = []
    for r in rows:
        s = trgm_similarity(r[4] or "", q)
        if s > threshold:
            scored.append((r[0], r[1], r[2], r[3], r[4], s))
    scored.sort(key=lambda t: -t[5])
    return scored[:limit]


# ---------------------------------------------------------------- 向量

def encode_vector(vec: list[float]) -> str:
    """向量的存储形式。两端都存文本，PG 侧再 CAST 成 vector 类型。

    `[1.0,2.0]` 这个形式同时是 pgvector 的字面量和合法 JSON，
    所以 SQLite 侧直接 json.loads 就能读回来，不需要两套编码。
    """
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def decode_vector(s: str | None) -> list[float] | None:
    if not s:
        return None
    try:
        v = json.loads(s)
        return v if isinstance(v, list) and v else None
    except Exception:
        return None


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


SQLITE_VEC_SCAN_CAP = 50000


async def vector_candidates(
    session: AsyncSession, backend: str, qvec: list[float], dim: int,
    where_sql: str, params: dict, limit: int,
) -> list[tuple]:
    """向量路候选集，返回 (chunk_id, doc_id, seq, heading, content, score)。

    score 是余弦相似度（1 最相似），两端一致 —— PG 侧用 `1 - (a <=> b)`
    换算，因为 pgvector 的 `<=>` 给的是余弦**距离**。
    """
    if backend == PG:
        sql = text(f"""
            SELECT c.id, c.document_id, c.seq, c.heading, c.content,
                   1 - (c.embedding::vector({dim}) <=> CAST(:vec AS vector({dim}))) AS score
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {where_sql} AND c.embedding IS NOT NULL
            ORDER BY c.embedding::vector({dim}) <=> CAST(:vec AS vector({dim}))
            LIMIT :lim
        """)
        return (await session.execute(
            sql, {**params, "vec": encode_vector(qvec), "lim": limit})).all()

    n = (await session.execute(text(
        f"SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        f"WHERE {where_sql} AND c.embedding IS NOT NULL"), params)).scalar() or 0
    if n > SQLITE_VEC_SCAN_CAP:
        return []
    rows = (await session.execute(text(
        f"SELECT c.id, c.document_id, c.seq, c.heading, c.content, c.embedding "
        f"FROM chunks c JOIN documents d ON d.id = c.document_id "
        f"WHERE {where_sql} AND c.embedding IS NOT NULL"), params)).all()
    scored = []
    for r in rows:
        v = decode_vector(r[5])
        # 维度不符的跳过而不是算出个错分数：换过 embedding 模型的库里
        # 会混着两种维度的旧向量，硬算等于拿噪声参与排序
        if not v or len(v) != dim:
            continue
        scored.append((r[0], r[1], r[2], r[3], r[4], cosine(v, qvec)))
    scored.sort(key=lambda t: -t[5])
    return scored[:limit]


# ---------------------------------------------------------------- FTS5 维护

async def write_chunk_lexemes(
    session: AsyncSession, backend: str, chunk_id: int, lex: str,
) -> None:
    """把扩展词串写进关键词索引。

    PG：更新 chunks.tsv（TSVECTOR 列）
    SQLite：写 chunks_fts 虚拟表

    两端的输入 `lex` 完全一样 —— 都是 expand_tokens() 的产物，
    这是两端召回可比的前提。
    """
    if backend == PG:
        await session.execute(
            text("UPDATE chunks SET tsv = to_tsvector('simple', :lex) WHERE id = :id"),
            {"lex": lex, "id": chunk_id},
        )
    else:
        # 先删后插：reindex 会重复调，不删会累积重复行，
        # 同一个 chunk 在 FTS 里出现 N 次 → bm25 分数虚高 N 倍
        await session.execute(
            text("DELETE FROM chunks_fts WHERE chunk_id = :id"), {"id": chunk_id})
        await session.execute(
            text("INSERT INTO chunks_fts(lex, chunk_id) VALUES (:lex, :id)"),
            {"lex": lex, "id": chunk_id},
        )


async def drop_chunk_lexemes(session: AsyncSession, backend: str, doc_id: int) -> None:
    """删文档时清掉它所有 chunk 的关键词索引行。

    PG 不需要这步：tsv 是 chunks 表的列，chunk 删掉就一起没了。
    SQLite 的 FTS5 是独立表，没有外键级联，必须显式删 ——
    漏了会留下指向已删 chunk 的 FTS 行，检索时 JOIN 不上就静默少结果，
    而 count(*) 又对不上，很难查。
    """
    if backend == SQLITE:
        await session.execute(text(
            "DELETE FROM chunks_fts WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE document_id = :d)"), {"d": doc_id})


async def rebuild_fts(session: AsyncSession, backend: str, lexer) -> int:
    """从 chunks 表全量重建 FTS5 索引（只 SQLite 需要）。

    什么时候用：手工改过 sqlite 文件、FTS 表被删、或怀疑索引和内容不一致。
    PG 侧没有对应操作 —— tsv 跟 chunk 同生共死，不会漂移。

    lexer 是 `(title, project, tags, heading, content) -> str` 的回调，
    由 service 层传进来（就是它写入时用的那个函数）。
    刻意不在 chunks 表上加一列存 lex：那等于把正文存两遍，
    而 lex 完全可以从正文重算 —— 派生数据不该有第二个真值来源。
    """
    if backend != SQLITE:
        return 0
    await session.execute(text("DELETE FROM chunks_fts"))
    rows = (await session.execute(text(
        "SELECT c.id, c.heading, c.content, d.title, d.project, d.tags "
        "FROM chunks c JOIN documents d ON d.id = c.document_id"))).all()
    n = 0
    for cid, heading, content, title, project, tags in rows:
        try:
            tag_list = json.loads(tags) if isinstance(tags, str) else (tags or [])
        except Exception:
            tag_list = []
        lex = lexer(title or "", project or "", tag_list, heading or "", content or "")
        if lex:
            await session.execute(
                text("INSERT INTO chunks_fts(lex, chunk_id) VALUES (:lex, :id)"),
                {"lex": lex, "id": cid})
            n += 1
    return n


# ---------------------------------------------------------------- 能力自检

async def capabilities(session: AsyncSession, backend: str) -> dict[str, Any]:
    """运行时能力自检，给 /api/v1/system 和启动日志用。

    这个接口存在的理由：本地模式少了 pg_trgm 的 GIN 索引和 hnsw 近邻索引，
    行为差异必须能被看见。用户报"本地搜得比服务器慢"时，第一件事是看这里。
    """
    out: dict[str, Any] = {"backend": backend}
    if backend == PG:
        try:
            rows = (await session.execute(text(
                "SELECT extname FROM pg_extension WHERE extname IN ('pg_trgm','vector')"))).all()
            ext = {r[0] for r in rows}
            out["fuzzy"] = "pg_trgm" if "pg_trgm" in ext else "unavailable"
            out["vector_index"] = "hnsw" if "vector" in ext else "unavailable"
        except Exception:
            out["fuzzy"] = out["vector_index"] = "unknown"
        out["keyword"] = "tsvector+gin"
    else:
        try:
            await session.execute(text("SELECT count(*) FROM chunks_fts"))
            out["keyword"] = "fts5+bm25"
        except Exception:
            out["keyword"] = "MISSING(chunks_fts 表不存在，重启服务会自动建)"
        out["fuzzy"] = f"python-trigram(全扫，上限 {SQLITE_TRGM_SCAN_CAP} chunk)"
        out["vector_index"] = f"python-bruteforce(全扫，上限 {SQLITE_VEC_SCAN_CAP} chunk)"
    try:
        out["chunk_count"] = (await session.execute(text("SELECT count(*) FROM chunks"))).scalar()
    except Exception:
        out["chunk_count"] = None
    return out
