#!/usr/bin/env python3
"""定位 28 chunk 的长文为什么向量整批失败。分批逻辑看着对，得看上游真实报错。"""
import asyncio, json, re, time

import httpx
from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import Document
from app.mdstore import split_chunks
from app.config import settings

BASE = settings.embed_api_base.rstrip("/") + "/embeddings"


async def raw_call(texts, tag):
    """不带重试不吞异常，直接看上游说什么。"""
    async with httpx.AsyncClient(timeout=60) as c:
        t0 = time.time()
        try:
            r = await c.post(BASE,
                headers={"Authorization": f"Bearer {settings.embed_api_key}"},
                json={"model": settings.embed_model, "input": texts,
                      "encoding_format": "float", "dimensions": settings.embed_dim})
            dt = time.time() - t0
            if r.status_code != 200:
                print(f"  {tag}: HTTP {r.status_code}  {r.text[:220]}")
                return None
            rows = r.json()["data"]
            print(f"  {tag}: ✅ {len(rows)} 条  {dt*1000:.0f}ms  维度 {len(rows[0]['embedding'])}")
            return rows
        except Exception as e:
            print(f"  {tag}: ❌ {type(e).__name__} {str(e)[:150]}")
            return None


async def main():
    async with SessionLocal() as s:
        d = (await s.execute(select(Document).where(
            Document.title.like("%frcws%"), Document.deleted_at.is_(None)
        ))).scalars().first()
        pieces = split_chunks(d.content)
        payload = [f"{d.title}\n{p['heading']}\n{p['content']}" for p in pieces]
        print(f"文档《{d.title[:40]}》 {len(d.content)} 字符 → {len(payload)} chunk")
        sizes = sorted(len(t) for t in payload)
        print(f"chunk 长度：最小 {sizes[0]}  中位 {sizes[len(sizes)//2]}  最大 {sizes[-1]}")
        clipped = [t[: settings.embed_max_chars] for t in payload]
        print(f"截断到 {settings.embed_max_chars} 后最大 {max(len(t) for t in clipped)}\n")

        print("1. 逐条试，找出是哪条有毒")
        bad = []
        for i, t in enumerate(clipped):
            r = await raw_call([t], f"chunk[{i}] len={len(t)}")
            if r is None:
                bad.append(i)
        print(f"  → 失败的 chunk: {bad if bad else '无，单条全部 OK'}\n")

        print("2. 按 25 分批试（复现 embed_texts 的行为）")
        for i in range(0, len(clipped), 25):
            b = clipped[i:i + 25]
            tot = sum(len(x) for x in b)
            await raw_call(b, f"batch[{i}:{i+len(b)}] {len(b)}条 共{tot}字符")

        print("\n3. 缩小批量找上限（怀疑是总 token 数而不是条数）")
        for bs in (25, 16, 10, 8, 5):
            b = clipped[:bs]
            tot = sum(len(x) for x in b)
            await raw_call(b, f"前{bs}条 共{tot}字符")

    await engine.dispose()


asyncio.run(main())
