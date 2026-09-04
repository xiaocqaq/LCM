#!/usr/bin/env python3
"""校准 est_tokens()：拿上游真实的 usage.total_tokens 对照我的估算。

为什么必须校准：est_tokens 低估 = 截断不够 = 请求撞上游 512 token 上限 =
ReadTimeout 挂死 20 秒。这个失败模式不报错、看着像网络抖动，
所以估算器的安全方向必须是**高估**。

用 text-embedding-v4 拿真实 token 数（它能吃 8000 字，不会因超限而超时）。
"""
import asyncio
import re
import sys

sys.path.insert(0, "/opt/memorys")

import httpx
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.search import est_tokens


async def real_tokens(s, tmo=15.0):
    base = settings.embed_api_base.rstrip("/") + "/embeddings"
    try:
        async with httpx.AsyncClient(timeout=tmo) as cl:
            r = await cl.post(
                base,
                headers={"Authorization": f"Bearer {settings.embed_api_key}"},
                json={"model": "text-embedding-v4", "input": [s],
                      "encoding_format": "float", "dimensions": 1024})
            if r.status_code == 200:
                return (r.json().get("usage") or {}).get("total_tokens")
    except Exception:
        pass
    return None


async def main():
    async with engine.connect() as c:
        rows = (await c.execute(text("""
            SELECT ch.id, ch.content
            FROM chunks ch JOIN documents d ON d.id = ch.document_id
            WHERE d.deleted_at IS NULL
            ORDER BY length(ch.content) DESC LIMIT 12"""))).all()

    print(f"{'id':>5} {'字符':>5} {'CJK':>5} {'实tok':>6} {'我估':>5} {'估/实':>6}")
    ratios = []
    for cid, cc in rows:
        u = await real_tokens(cc)
        if not u:
            print(f"{cid:>5} {len(cc):>5}     -      -     - （上游没返回，跳过）")
            continue
        mine = est_tokens(cc)
        cjk = len(re.findall(r"[\u4e00-\u9fff]", cc))
        ratios.append(mine / u)
        flag = "" if mine >= u else "  ← 低估！"
        print(f"{cid:>5} {len(cc):>5} {cjk:>5} {u:>6} {mine:>5} {mine/u:>6.2f}{flag}")

    if ratios:
        lo, hi = min(ratios), max(ratios)
        print(f"\n估/实 比值：min={lo:.2f}  max={hi:.2f}")
        if lo < 1.0:
            print("❌ 存在低估 —— est_tokens 必须调高，否则长 chunk 会挂死上游")
            print(f"   建议把 embed_max_tokens 从 {settings.embed_max_tokens} "
                  f"降到 {int(settings.embed_max_tokens * lo * 0.95)}")
        else:
            print("✅ 全部高估或持平，截断方向安全")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
