#!/usr/bin/env python3
"""向量救回的 3 条，加同义词能不能也救回？

这是"向量有必要吗"的关键证据：如果几行同义词表就能解决，
那 1024 维向量 + 每次检索一次网络往返就是大炮打蚊子。
"""
import asyncio

from sqlalchemy import select
from app.db import SessionLocal, engine
from app.models import User
from app import search as S
from app import service as SV

CASES = [
    ("数据存在哪里", "memorys"),
    ("怎么换语音合成服务", "TTS"),
    ("语音朗读用哪个服务", "TTS"),
]

# 补这几条就够覆盖上面 3 个查询吗？
PATCH = {
    "数据": ("存储", "markdown", "md", "落盘", "文件"),
    "存": ("存储", "落盘", "保存"),
    "语音": ("tts", "朗读", "合成", "音频"),
    "朗读": ("tts", "语音", "合成"),
    "合成": ("tts", "语音"),
}


async def probe(s, u, label):
    out = []
    for q, want in CASES:
        r = await SV.search_chunks(s, u, q, limit=5, use_vector=False)
        titles = [x["doc"]["title"] for x in r]
        hit = any(want.lower() in t.lower() for t in titles)
        top1 = bool(titles) and want.lower() in titles[0].lower()
        out.append((q, want, hit, top1, titles[:2]))
    print(f"--- {label} ---")
    for q, want, hit, top1, t in out:
        print(f"  {'✅' if hit else '❌'} top1={'✅' if top1 else '❌'}  {q:<20} 期望={want:<10} {t}")
    return out


async def main():
    async with SessionLocal() as s:
        u = (await s.execute(select(User).limit(1))).scalars().first()

        print("=" * 74)
        print("A. 当前同义词表（纯关键词两路）")
        print("=" * 74)
        before = await probe(s, u, "改之前")

        print()
        print("=" * 74)
        print("B. 加了 5 条同义词后（仍是纯关键词两路，需要 reindex 才生效）")
        print("=" * 74)
        for k, v in PATCH.items():
            S._SYNONYMS[k] = tuple(set(S._SYNONYMS.get(k, ()) + v))
        # 索引侧扩展改了必须重建索引
        docs = (await s.execute(select(SV.Document).where(
            SV.Document.deleted_at.is_(None)))).scalars().all()
        for d in docs:
            await SV._reindex_document(s, d, do_embed=False)
        await s.commit()
        print(f"  （已重建 {len(docs)} 篇的关键词索引，不重算向量）\n")
        after = await probe(s, u, "加同义词后")

        print()
        print("=" * 74)
        print("C. 三路（向量）对照")
        print("=" * 74)
        for q, want in CASES:
            r = await SV.search_chunks(s, u, q, limit=5)
            titles = [x["doc"]["title"] for x in r]
            hit = any(want.lower() in t.lower() for t in titles)
            top1 = bool(titles) and want.lower() in titles[0].lower()
            print(f"  {'✅' if hit else '❌'} top1={'✅' if top1 else '❌'}  {q:<20} "
                  f"期望={want:<10} {titles[:2]}")

        print()
        print("=" * 74)
        nb = sum(1 for x in before if x[2])
        na = sum(1 for x in after if x[2])
        print(f"同义词方案：{nb}/3 → {na}/3")
        if na == 3:
            print("→ 同义词就能全部救回，向量在这 3 条上没有独占价值")
        elif na > nb:
            print(f"→ 同义词能救回 {na-nb} 条，剩 {3-na} 条仍需向量")
        else:
            print("→ 同义词救不回来，这就是向量的独占价值")

    await engine.dispose()


asyncio.run(main())
