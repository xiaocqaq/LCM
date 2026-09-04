#!/usr/bin/env python3
"""两端一致性：SQLite 和 PostgreSQL 的检索结果是否可比。

这套测试要回答一个具体问题：**同一个查询，本地模式和服务器模式给出的答案一样吗？**

如果不一样，用户会以为数据没同步 —— 那是最难自证清白的一类 bug。

## 检查两层

1. **纯函数层（不需要数据库）**：`dialect.trgm_similarity()` 是 `pg_trgm.similarity()`
   的 Python 复刻。把已知的三元组切分行为逐条对照，任何偏差都会让模糊路的
   召回集在两端不同。这一层可以离线跑。

2. **端到端层（需要两个实例都在跑）**：同一批文档写进两个后端，同一批查询，
   比较 top1 是否同一篇、top3 集合的重合度。

第 2 层的**期望不是 100% 一致**，理由写在 assert 附近：两端的关键词打分函数
本质不同（bm25 vs ts_rank），排名细节必然有差异。要求 top1 一致、
top3 交集 ≥2 —— 这个标准的含义是"用户问同一个问题，两边都能找到那篇文档"，
而不是"两个数据库实现字节级相同"。

跑法：
    # 纯函数层（随时可跑）
    .venv/bin/python tests/local_parity.py --units-only

    # 端到端层（需要 PG 侧和 SQLite 侧都在跑）
    PG_BASE=https://repo.xlingo.fun PG_KEY=hk_xxx \\
    SQLITE_BASE=http://127.0.0.1:8650 \\
      .venv/bin/python tests/local_parity.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK = FAIL = 0


def chk(cond, name, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  → {extra}" if extra else ""))


# ---------------------------------------------------------------- 第 1 层：纯函数
def test_units():
    print("\n[1] pg_trgm 复刻的正确性（离线）")
    from app.dialect import _trigrams, trgm_similarity
    # PG 的 show_trgm('abc') 实测返回 {"  a"," ab","abc","bc "}：
    # 词前补两个空格、后补一个空格，再滑窗取 3。
    # 这个补白规则不是随便定的 —— 它让短词也能产生足够多的三元组，
    # 抄错的话短查询（2-3 字）的相似度会系统性偏低。
    chk(_trigrams("abc") == {"  a", " ab", "abc", "bc "},
        "三元组切分与 show_trgm('abc') 一致", str(sorted(_trigrams("abc"))))

    # 非字母数字当分隔符，切成多个词各自补白。
    # 单字词 'a' → "  a " → {"  a", " a "}，所以 'a-b' 是 4 个而不是 2 个。
    # 实测对照过 `SELECT show_trgm('a-b')`，PG 给的正是这 4 个。
    got = _trigrams("a-b")
    chk(got == {"  a", " a ", "  b", " b "},
        "分隔符切词（'a-b' → 4 个三元组）", str(sorted(got)))

    chk(trgm_similarity("abc", "abc") == 1.0, "完全相同 → 1.0")
    chk(trgm_similarity("abc", "xyz") == 0.0, "完全无关 → 0.0")
    s = trgm_similarity("部署流程", "部署")
    chk(0 < s < 1, f"部分重叠落在开区间（{s:.3f}）", f"{s}")

    # 对称性：similarity(a,b) == similarity(b,a)。
    # Jaccard 本身对称，但如果实现里写成 inter/len(ta) 就不对称了 ——
    # 那种错误在单侧测试里看不出来。
    a, b = "postgres 连接串", "连接串 postgres"
    chk(abs(trgm_similarity(a, b) - trgm_similarity(b, a)) < 1e-9, "对称性")

    # 空输入不能炸也不能返回 nan
    chk(trgm_similarity("", "abc") == 0.0, "空串 → 0.0（不抛异常）")

    print("\n[2] FTS5 查询表达式的安全性")
    from app.dialect import fts5_match_expr

    # FTS5 语法里 - 是 NOT、* 是前缀、: 是列限定、" 是短语界定。
    # jieba 切出来的词带这些字符时，不处理会让整条 MATCH 语义变掉甚至报错。
    e = fts5_match_expr(["fsdp-service", "端口", "a*b", 'x"y'])
    chk("-" not in e and "*" not in e, "危险字符已剔除", e)
    chk(e.count('"') % 2 == 0, "引号成对", e)
    chk(" OR " in e, "多词用 OR 连接（对应 PG 的 tsquery |）", e)

    # OR 而不是 AND 是刻意的：AND 语义下"memorys 怎么部署"要求三词全中，
    # 而"怎么"已被停用词表滤掉，剩下两词全中的 chunk 可能一个都没有。
    single = fts5_match_expr(["部署"])
    chk(" OR " not in single, "单词不带 OR", single)
    chk(fts5_match_expr([]) == "", "空词表 → 空表达式（调用方据此跳过这一路）")
    chk(fts5_match_expr(["a"]) == "", "单字符词被滤掉（无区分度）")

    print("\n[3] 向量编解码在两端可互换")
    from app.dialect import cosine, decode_vector, encode_vector

    v = [0.1, -0.25, 0.5]
    s = encode_vector(v)
    # 这个字面量必须同时是 pgvector 字面量和合法 JSON —— 否则两端要两套编码，
    # 而向量数据在两边迁移时就会静默错位
    chk(s.startswith("[") and s.endswith("]"), "编码成 [..] 字面量", s)
    chk(json.loads(s), "同时是合法 JSON（SQLite 侧直接 json.loads 读回）")
    back = decode_vector(s)
    chk(back is not None and len(back) == 3 and abs(back[0] - 0.1) < 1e-6,
        "解码往返一致", str(back))
    chk(decode_vector(None) is None and decode_vector("") is None, "空值安全")
    chk(decode_vector("not-json") is None, "坏数据返回 None 而不是抛异常")
    chk(abs(cosine([1, 0], [1, 0]) - 1.0) < 1e-9, "余弦：同向 → 1")
    chk(abs(cosine([1, 0], [0, 1])) < 1e-9, "余弦：正交 → 0")
    chk(cosine([1, 0], [1, 0, 0]) == 0.0, "维度不符 → 0（不参与排序）")
    chk(cosine([0, 0], [1, 1]) == 0.0, "零向量不除零")

    print("\n[3b] pg_trgm 数值对照（连真实 PG）")
    _compare_with_pg()


def _compare_with_pg():
    """拿真实 PG 的 similarity() 逐条对照 Python 实现。

    这一段才是"复刻正确"的真凭据。上面几条断言只能证明"实现自洽"，
    不能证明"跟 PG 一样" —— 而后者是本地模式可用的前提。

    连不上 PG 就跳过而不是失败：这个脚本要能在只有 SQLite 的机器上跑
    （那正是本地模式的场景）。
    """
    import asyncio

    try:
        from app.config import DB_BACKEND
        from app.db import engine
        from app.dialect import PG, trgm_similarity
    except Exception as e:
        print(f"       跳过：导入失败 {e}")
        return
    if DB_BACKEND != PG:
        print("       跳过：当前 MEM_DATABASE_URL 不是 PostgreSQL")
        return

    PAIRS = [
        ("部署流程", "部署"),
        ("postgres 连接串", "连接串 postgres"),
        ("abc", "abd"),
        ("向量检索接入", "向量检索"),
        ("fsdp-service 工程结构", "fsdp service 结构"),
        ("sqlite 本地模式", "本地模式 sqlite"),
    ]

    async def run():
        from sqlalchemy import text as _text
        async with engine.connect() as c:
            for a, b in PAIRS:
                pg = float((await c.execute(
                    _text("SELECT similarity(:a,:b)"), {"a": a, "b": b})).scalar())
                mine = trgm_similarity(a, b)
                # 1e-6 而不是精确相等：PG 的 similarity 返回 real（单精度），
                # Python 算的是双精度，末位必然有差
                chk(abs(pg - mine) < 1e-6,
                    f"similarity({a[:12]!r}, {b[:12]!r}) = {pg:.6f}",
                    f"PG={pg:.8f} mine={mine:.8f} Δ={abs(pg - mine):.2e}")
        await engine.dispose()

    try:
        asyncio.run(run())
    except Exception as e:
        print(f"       跳过：连不上 PG（{str(e)[:80]}）")


# ---------------------------------------------------------------- 第 2 层：端到端
def req(base, method, path, body=None, key=None):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if key:
        h["X-Api-Key"] = key
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


SFX = "-parity8841"
FIXTURES = [
    (f"部署流程{SFX}", "howto", "paritytest",
     "# 装依赖\n\n先装 Python 依赖。\n\n# 起服务\n\nuvicorn 监听 8649 端口。\n\n"
     "# 反代\n\nnginx 把 /mem 前缀剥掉转发到本机。\n"),
    (f"数据库选型{SFX}", "decision", "paritytest",
     "# 结论\n\n服务器用 PostgreSQL，本地用 SQLite。\n\n"
     "# 理由\n\nPG 有 tsvector 和 pgvector；SQLite 零配置。\n"),
    (f"向量检索接入{SFX}", "fact", "paritytest",
     "# 现状\n\n向量走阿里云百炼，1024 维。\n\n"
     "# 效果\n\ntop5 召回从 80% 提到 100%，但 top1 反而降了。\n"),
    (f"检索排序细节{SFX}", "fact", "paritytest",
     "# 长度归一化\n\n长文靠体量刷排名，要按文档长度衰减。\n\n"
     "# RRF\n\n三路等权融合，谁都不该压倒另外两个。\n"),
]

QUERIES = [
    "怎么部署",
    "为什么用 sqlite",
    "向量检索效果怎么样",
    "长文排名问题",
    "nginx 前缀",
]


def seed(base, key):
    ids = []
    for title, typ, proj, content in FIXTURES:
        st, d = req(base, "POST", "/api/v1/documents",
                    {"title": title, "type": typ, "project": proj,
                     "content": content, "importance": 4}, key)
        if st == 200 and d.get("id"):
            ids.append(d["id"])
    return ids


def probe(base, key, q):
    """返回按排名去重后的**文档**标题序列。

    必须去重：一篇文档最多返回 PER_DOC_CAP=2 个 chunk，
    原始结果里同一个标题会出现两次。第一版直接拿 `titles[:3]` 做集合比较，
    于是 set 去重后只剩 1 个元素，"top3 交集 1/3" 永远失败 ——
    那是断言的 bug，不是两端不一致。
    """
    st, r = req(base, "GET",
                f"/api/v1/search?q={urllib.parse.quote(q)}&project=paritytest"
                f"&limit=8&mode=keyword", None, key)
    # mode=keyword 而不是 hybrid：向量路要调外部 API，本地实例通常没配，
    # 带上它比的就不是同一件事了。关键词 + 模糊两路才是两端都必然可用的部分。
    seen, out = set(), []
    for h in (r.get("results") or []):
        t = h["doc"]["title"]
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def cleanup(base, key, ids):
    for i in ids:
        req(base, "DELETE", f"/api/v1/documents/{i}", None, key)


def test_e2e():
    pg_base = os.environ.get("PG_BASE", "")
    lite_base = os.environ.get("SQLITE_BASE", "http://127.0.0.1:8650")
    # Key 优先取环境变量；没给就读服务器上那份（跟 smoke_ready.py 一致）。
    # 不要去 mint 新 Key —— 那会在生产库里堆一堆一次性凭据。
    pg_key = os.environ.get("PG_KEY", "")
    if not pg_key:
        try:
            import pathlib
            pg_key = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
        except Exception:
            pg_key = ""
    if not pg_base or not pg_key:
        print("\n[4] 端到端一致性  —— 跳过（未设 PG_BASE，或读不到 API Key）")
        print("     用法：PG_BASE=https://… [PG_KEY=hk_…] python tests/local_parity.py")
        return

    print(f"\n[4] 端到端一致性  PG={pg_base}  SQLite={lite_base}")
    st, _ = req(lite_base, "GET", "/api/health")
    if st != 200:
        chk(False, f"SQLite 实例不可达（{lite_base}）", "先起本地实例")
        return
    st, _ = req(pg_base, "GET", "/api/health")
    if st != 200:
        chk(False, f"PG 实例不可达（{pg_base}）")
        return

    pg_ids = seed(pg_base, pg_key)
    lite_ids = seed(lite_base, None)
    chk(len(pg_ids) == len(FIXTURES), f"PG 侧写入 {len(pg_ids)}/{len(FIXTURES)} 篇")
    chk(len(lite_ids) == len(FIXTURES), f"SQLite 侧写入 {len(lite_ids)}/{len(FIXTURES)} 篇")

    try:
        top1_same = 0
        for q in QUERIES:
            a = probe(pg_base, pg_key, q)
            b = probe(lite_base, None, q)
            if not a or not b:
                chk(False, f"「{q}」两端都要有结果", f"PG={len(a)} SQLite={len(b)}")
                continue
            same1 = a[0] == b[0]
            top1_same += int(same1)
            inter = len(set(a[:3]) & set(b[:3]))
            need = min(3, len(a), len(b))     # 只有 1 篇命中时，交集上限就是 1
            # 标准是"用户问同一个问题，两边都能找到那篇文档"，
            # 不是"两个数据库实现字节级相同"—— bm25 和 ts_rank 是不同的打分函数，
            # 排名细节必然有差异，强求全等只会得到一个永远在闪的测试。
            chk(inter >= need, f"「{q}」top{need} 命中集合一致（{inter}/{need}）"
                               f"{'' if same1 else ' ⚠ top1 不同'}",
                f"PG={[t[:14] for t in a[:3]]} / SQLite={[t[:14] for t in b[:3]]}")
        rate = top1_same / len(QUERIES)
        chk(rate >= 0.6, f"top1 一致率 {top1_same}/{len(QUERIES)} = {rate:.0%}（阈值 60%）")
    finally:
        cleanup(pg_base, pg_key, pg_ids)
        cleanup(lite_base, None, lite_ids)
        print("     已清理两端的测试文档")


if __name__ == "__main__":
    print("\n===== 两端一致性 =====")
    test_units()
    if "--units-only" not in sys.argv:
        test_e2e()
    print(f"\n===== OK={OK}  FAIL={FAIL} =====")
    sys.exit(1 if FAIL else 0)
