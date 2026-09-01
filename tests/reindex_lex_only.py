#!/usr/bin/env python3
"""只重建关键词索引（tsv），保留已有向量。

用途：改了 _SYNONYMS/_EN2ZH 之后要让索引侧扩展生效，但不想重跑 7 分钟的向量。
坑：不能用 _reindex_document(do_embed=False) —— 那会 DELETE 掉整行 chunk
再重插，向量一起没了（本轮踩过，60 个向量被清空）。
所以这里只 UPDATE tsv 列，不动 chunk 行本身。
"""
import asyncio

from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import Chunk, Document
from app.service import _tsvector_literal


async def main():
    async with SessionLocal() as s:
        docs = (await s.execute(select(Document).where(
            Document.deleted_at.is_(None)))).scalars().all()
        n = 0
        for d in docs:
            chunks = (await s.execute(select(Chunk).where(
                Chunk.document_id == d.id).order_by(Chunk.seq))).scalars().all()
            for ch in chunks:
                lex = _tsvector_literal(
                    f"{d.title} {d.project} {' '.join(d.tags or [])} "
                    f"{ch.heading} {ch.content}")
                await s.execute(
                    text("UPDATE chunks SET tsv = to_tsvector('simple', :lex) WHERE id = :id"),
                    {"lex": lex, "id": ch.id})
                n += 1
        await s.commit()
        r = (await s.execute(text("SELECT count(*), count(embedding) FROM chunks"))).one()
        print(f"重建 {n} 个 chunk 的关键词索引")
        print(f"向量保持不变：{r[1]}/{r[0]}")
    await engine.dispose()


asyncio.run(main())
