"""诊断：user 18 的文档是否真的建了 chunk 索引 / tsv 是否为空。"""
import asyncio

from sqlalchemy import text

from app.db import SessionLocal
from app.search import tokenize


async def main():
    async with SessionLocal() as s:
        rows = (await s.execute(text("""
            SELECT d.id, d.title, count(c.id) AS chunks,
                   count(c.tsv) AS with_tsv,
                   coalesce(sum(length(c.content)), 0) AS content_len
            FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
            WHERE d.user_id = 18
            GROUP BY d.id, d.title ORDER BY d.id
        """))).all()
        print("doc_id | chunks | with_tsv | content_len | title")
        for r in rows:
            print(f"{r[0]:6} | {r[2]:6} | {r[3]:8} | {r[4]:11} | {r[1]}")

        print("\n--- 某个 chunk 的 tsv 前 200 字符 ---")
        r = (await s.execute(text("""
            SELECT c.id, c.document_id, left(c.content, 80), left(c.tsv::text, 200)
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.user_id = 18 ORDER BY c.id LIMIT 3
        """))).all()
        for x in r:
            print(f"chunk={x[0]} doc={x[1]}")
            print(f"  content: {x[2]!r}")
            print(f"  tsv    : {x[3]!r}")

        print("\n--- tokenize 出来的查询词 ---")
        for q in ["记忆", "memorys", "RRF 融合", "PG 连接"]:
            print(f"  {q!r} -> {tokenize(q)}")

        print("\n--- 直接用 tsv @@ to_tsquery 手工验证 ---")
        toks = tokenize("记忆")
        tq = " | ".join(toks) if toks else "记忆"
        got = (await s.execute(text("""
            SELECT c.id, d.title FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.user_id = 18 AND c.tsv @@ to_tsquery('simple', :tq) LIMIT 5
        """), {"tq": tq})).all()
        print(f"  to_tsquery('simple', {tq!r}) -> {got}")


asyncio.run(main())
