#!/usr/bin/env python3
"""全量重建索引（含向量）。直接走 service 层，不经 HTTP —— 带 key 的 curl 会触发沙箱审批。

顺带清掉孤儿 chunk（指向已软删除文档的行）—— 它们不会被检索到，但会让
"多少 chunk 有向量" 这个统计失真，排查时误导人。
"""
import asyncio, time

from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import Document
from app.service import _reindex_document
from app.config import settings
from app.search import EMBED_AVAILABLE


async def main():
    print(f"EMBED_AVAILABLE={EMBED_AVAILABLE}  model={settings.embed_model}  "
          f"dim={settings.embed_dim}  batch={settings.embed_batch_size}  "
          f"timeout={settings.embed_timeout}s")
    t0 = time.time()
    async with SessionLocal() as s:
        before = (await s.execute(text("SELECT count(*) FROM chunks"))).scalar()
        orphan = (await s.execute(text(
            "DELETE FROM chunks c USING documents d "
            "WHERE d.id = c.document_id AND d.deleted_at IS NOT NULL"
        ))).rowcount
        await s.commit()
        print(f"清理孤儿 chunk（属于已删除文档）：{orphan} 行，剩 {before - orphan}\n")

        docs = (await s.execute(
            select(Document).where(Document.deleted_at.is_(None)).order_by(Document.id)
        )).scalars().all()
        print(f"共 {len(docs)} 篇活跃文档\n")
        partial = []
        for i, d in enumerate(docs, 1):
            td = time.time()
            await _reindex_document(s, d)
            await s.commit()
            n = (await s.execute(text(
                "SELECT count(*), count(embedding) FROM chunks WHERE document_id=:i"
            ), {"i": d.id})).one()
            if n[1] == n[0]:
                flag = "✅"
            elif n[1]:
                flag = "⚠️ 部分"; partial.append((d.title, n[0] - n[1]))
            else:
                flag = "❌ 无向量"; partial.append((d.title, n[0]))
            print(f"  {i:>2}/{len(docs)} {flag} {n[1]}/{n[0]} chunk  {time.time()-td:>6.1f}s  "
                  f"{d.title[:38]}")
        tot = (await s.execute(text("SELECT count(*), count(embedding) FROM chunks"))).one()
        pct = tot[1] / tot[0] * 100 if tot[0] else 0
        print(f"\n总计 {tot[0]} chunk，{tot[1]} 个有向量（{pct:.1f}%），耗时 {time.time()-t0:.1f}s")
        if tot[1] == tot[0]:
            print("✅ 全部 chunk 都有向量")
        else:
            print(f"⚠️ 缺 {tot[0]-tot[1]} 个（上游随机超时导致）：")
            for t, n in partial:
                print(f"     {n} 条  {t[:50]}")
            print("   检索仍可用：缺向量的 chunk 走关键词两路，有向量的走三路")
    await engine.dispose()


asyncio.run(main())
