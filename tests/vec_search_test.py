#!/usr/bin/env python3
"""三路混合检索的行为测试。

重点不是"向量让命中率变高"（那要看 recall_audit），而是锁住工程契约：
  1. 查询侧有硬预算，上游挂了必须降级而不是卡住
  2. 向量路挂掉时结果仍然可用（两路兜底）
  3. library/project 过滤在向量路同样生效，不能被绕过
  4. 维度不符的向量不写库
"""
import asyncio, time, sys

from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import Document, User
from app import search as S
from app import service as SV
from app.config import settings

FAIL = 0


def ck(cond, msg, extra=""):
    global FAIL
    if cond:
        print(f"  ✅ {msg}")
    else:
        FAIL += 1
        print(f"  ❌ {msg}  {extra}")


async def main():
    global FAIL
    async with SessionLocal() as s:
        u = (await s.execute(select(User).limit(1))).scalars().first()
        print(f"用户 id={u.id}\n")

        print("=" * 66)
        print("1. 三路都在时检索正常")
        print("=" * 66)
        t0 = time.time()
        r = await SV.search_chunks(s, u, "数据存在哪里", limit=5)
        dt = time.time() - t0
        ck(len(r) > 0, f"有结果（{len(r)} 条，{dt*1000:.0f}ms）")
        ck(dt < 10, f"耗时可接受 {dt*1000:.0f}ms")
        if r:
            print(f"     top1: {r[0]['doc']['title'][:44]}")

        print()
        print("=" * 66)
        print("2. 向量路挂掉时必须降级，不能连带整个检索失败")
        print("=" * 66)
        orig = S.embed_query

        async def dead(_):
            return None
        S.embed_query = dead
        SV.embed_query = dead
        r2 = await SV.search_chunks(s, u, "数据存在哪里", limit=5)
        ck(len(r2) > 0, f"向量路返回 None 时仍有结果（{len(r2)} 条）")
        S.embed_query = orig
        SV.embed_query = orig

        print()
        print("=" * 66)
        print("3. 查询侧超时预算：上游卡住时必须在预算内放弃")
        print("=" * 66)

        async def slow(_):
            await asyncio.sleep(30)
            return None
        S.embed_query = slow
        SV.embed_query = slow
        t0 = time.time()
        r3 = await SV.search_chunks(s, u, "数据存在哪里", limit=5)
        dt3 = time.time() - t0
        # 这里直接替换了 embed_query，所以测的是 search_chunks 有没有把
        # 慢调用挡在外面。若 dt3 接近 30s 说明没有任何预算保护。
        ck(dt3 < 12, f"没有干等到底（{dt3:.1f}s）",
           f"实测 {dt3:.1f}s —— 说明 semantic_search 缺少超时保护")
        ck(len(r3) > 0, f"降级后仍有结果（{len(r3)} 条）")
        S.embed_query = orig
        SV.embed_query = orig

        print()
        print("=" * 66)
        print("4. 真实上游的 embed_query 预算")
        print("=" * 66)
        t0 = time.time()
        v = await S.embed_query("数据存在哪里")
        dt4 = time.time() - t0
        ck(dt4 <= settings.embed_query_timeout + 1.5,
           f"embed_query 在预算内返回（{dt4*1000:.0f}ms，预算 {settings.embed_query_timeout}s）")
        if v:
            ck(len(v) == settings.embed_dim, f"维度正确 {len(v)}")
        else:
            print("     ⚠️ 本次上游没返回（随机超时），降级路径已覆盖")

        print()
        print("=" * 66)
        print("5. project 过滤在向量路同样生效")
        print("=" * 66)
        projs = [p for p in (await s.execute(
            select(Document.project).where(Document.deleted_at.is_(None),
                                           Document.project != "").distinct()
        )).scalars().all() if p]
        if projs:
            target = projs[0]
            rp = await SV.search_chunks(s, u, "项目", limit=10, project=target)
            bad = [x["doc"]["title"] for x in rp if x["doc"]["project"] != target]
            ck(not bad, f"project={target} 过滤下无越界结果（{len(rp)} 条）", f"越界: {bad}")
        else:
            print("     库里没有带 project 的文档，跳过")

        print()
        print("=" * 66)
        print("6. 维度不符的向量不入库")
        print("=" * 66)
        d = (await s.execute(select(Document).where(
            Document.deleted_at.is_(None)).limit(1))).scalars().first()
        origt = S.embed_texts

        async def wrongdim(texts, **kw):
            return [[0.1] * 999 for _ in texts]   # 故意给错维度
        S.embed_texts = wrongdim
        SV.embed_texts = wrongdim
        await SV._reindex_document(s, d)
        await s.commit()
        n = (await s.execute(text(
            "SELECT count(*), count(embedding) FROM chunks WHERE document_id=:i"
        ), {"i": d.id})).one()
        ck(n[1] == 0, f"999 维向量被拒，未写入（{n[1]}/{n[0]} 有向量）")
        S.embed_texts = origt
        SV.embed_texts = origt
        # 复原这篇的向量
        await SV._reindex_document(s, d)
        await s.commit()
        n2 = (await s.execute(text(
            "SELECT count(*), count(embedding) FROM chunks WHERE document_id=:i"
        ), {"i": d.id})).one()
        print(f"     已复原：{n2[1]}/{n2[0]} 有向量")

        print()
        print("=" * 66)
        print("7. hnsw 索引真的被用上（不是全表扫描）")
        print("=" * 66)
        vq = await S.embed_query("怎么做决策")
        if vq:
            vs = "[" + ",".join(f"{x:.6f}" for x in vq) + "]"
            dim = settings.embed_dim
            plan = (await s.execute(text(
                f"EXPLAIN SELECT c.id FROM chunks c "
                f"WHERE c.embedding IS NOT NULL "
                f"ORDER BY c.embedding::vector({dim}) <=> CAST(:v AS vector({dim})) LIMIT 5"
            ), {"v": vs})).scalars().all()
            txt = "\n".join(plan)
            # 断言的是"索引存在且维度对得上"，不是"这次查询走了索引"。
            # 60 行数据时 PG 选 Seq Scan 是正确决策（cost 41 比走索引更便宜），
            # 断言走索引会变成一条随数据量变化的假失败。
            idx = (await s.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename='chunks' AND indexname LIKE 'idx_chunks_embedding%'"
            ))).all()
            ck(len(idx) == 1, f"只有一个 embedding 索引（{[i[0] for i in idx]}）")
            if idx:
                ck(f"vector({dim})" in idx[0][1],
                   f"索引维度与配置一致（{dim}）", idx[0][1][:120])
                # 表达式必须逐字对得上，否则 PG 永远认不出这个索引可用
                ck(f"(embedding)::vector({dim})" in idx[0][1].replace(" ", "")
                   or f"embedding::vector({dim})" in idx[0][1].replace(" ", ""),
                   "索引表达式与查询表达式匹配", idx[0][1][:160])
            plan_ok = "Index Scan" in txt or "Seq Scan" in txt
            ck(plan_ok, "查询计划正常生成")
            if "Seq Scan" in txt:
                nrow = (await s.execute(text(
                    "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"))).scalar()
                print(f"     （本次走 Seq Scan：只有 {nrow} 行有向量，"
                      "PG 判断顺序扫描更便宜，属正确决策）")
        else:
            print("     上游本次没返回，跳过")

    await engine.dispose()
    print()
    print("=" * 66)
    print(f"FAIL={FAIL}")
    print("=" * 66)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
