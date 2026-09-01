#!/usr/bin/env python3
"""两路 vs 三路的 A/B 对比：向量检索到底带来多少增益。

同一套查询、同一个库，只切 use_vector 开关。
这是回答"这个向量模型值不值得用"的直接证据。
"""
import asyncio, sys

from sqlalchemy import select
from app.db import SessionLocal, engine
from app.models import User
from app import service as SV

# (查询, 期望命中的文档标题关键片段)。None = 库里确实没有答案，不该强求。
CASES = [
    ("数据存在哪里", "memorys"),
    ("怎么换语音合成服务", "TTS"),
    ("前端权限报错弹窗", "requireSuperAdmin"),
    ("大数据查询慢怎么优化", "frcws"),
    ("新人怎么快速了解这个项目", "xspeak"),
    ("什么时候该给用户提示", "引导时机"),
    ("换电脑后环境不一样", "Windows"),
    ("进程socket没了怎么恢复", "socket"),
    ("分支合并后要做什么", "del_ad"),
    ("自定义前端怎么发版", "hs.xlingo.fun"),
    # 同义改写：正文里用的是别的说法
    ("语音朗读用哪个服务", "TTS"),
    ("弹窗风暴", "requireSuperAdmin"),
    ("换行符问题", "Windows"),
    ("知识库怎么部署的", "memorys"),
    ("查询优化", "frcws"),
]


async def run(s, u, q, use_vector):
    r = await SV.search_chunks(s, u, q, limit=5, use_vector=use_vector)
    return [x["doc"]["title"] for x in r]


async def main():
    async with SessionLocal() as s:
        u = (await s.execute(select(User).limit(1))).scalars().first()
        kw_hit = hy_hit = 0
        rows = []
        for q, want in CASES:
            a = await run(s, u, q, False)
            b = await run(s, u, q, True)
            ha = any(want.lower() in t.lower() for t in a)
            hb = any(want.lower() in t.lower() for t in b)
            kw_hit += ha
            hy_hit += hb
            # top1 是否命中（更严格）
            t1a = bool(a) and want.lower() in a[0].lower()
            t1b = bool(b) and want.lower() in b[0].lower()
            rows.append((q, want, ha, hb, t1a, t1b, a[:1], b[:1]))

        print(f"{'查询':<22} {'期望':<20} 两路 三路  两路top1 三路top1")
        print("-" * 78)
        for q, want, ha, hb, t1a, t1b, a, b in rows:
            m = lambda x: "✅" if x else "❌"
            flag = ""
            if hb and not ha:
                flag = "  ← 向量救回"
            elif ha and not hb:
                flag = "  ← 向量弄丢"
            elif t1b and not t1a:
                flag = "  ← 向量提到第一"
            print(f"{q:<22} {want:<20} {m(ha)}   {m(hb)}    {m(t1a)}      {m(t1b)}{flag}")

        n = len(CASES)
        t1kw = sum(1 for r in rows if r[4])
        t1hy = sum(1 for r in rows if r[5])
        print()
        print("=" * 78)
        print(f"top5 命中率   两路 {kw_hit}/{n} = {kw_hit/n*100:.0f}%   "
              f"三路 {hy_hit}/{n} = {hy_hit/n*100:.0f}%   "
              f"增益 {(hy_hit-kw_hit)/n*100:+.0f}pt")
        print(f"top1 命中率   两路 {t1kw}/{n} = {t1kw/n*100:.0f}%   "
              f"三路 {t1hy}/{n} = {t1hy/n*100:.0f}%   "
              f"增益 {(t1hy-t1kw)/n*100:+.0f}pt")
        print("=" * 78)
        saved = [r[0] for r in rows if r[3] and not r[2]]
        lost = [r[0] for r in rows if r[2] and not r[3]]
        if saved:
            print(f"向量救回：{saved}")
        if lost:
            print(f"向量弄丢：{lost}  ← 有丢的说明融合权重要调")
    await engine.dispose()


asyncio.run(main())
