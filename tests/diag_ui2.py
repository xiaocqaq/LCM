"""用种子数据里真实存在的词验证检索，确认前面 0 命中是查询词不匹配而非 bug。"""
import asyncio
import os

import httpx

BASE = os.environ.get("MEM_TEST_BASE", "http://127.0.0.1:8649").rstrip("/")
TOKEN = open("/tmp/mem_ui_token.txt").read().strip()


async def main():
    h = {"Authorization": "Bearer " + TOKEN}
    async with httpx.AsyncClient(timeout=30, headers=h) as c:
        for q in ["memorys 是什么", "RRF 融合怎么做的", "PG 端口是多少",
                  "怎么挂到 agent", "备份", "架构"]:
            r = await c.get(BASE + "/api/v1/search", params={"q": q, "limit": 5})
            d = r.json()
            hits = d.get("results") or []
            names = [(x.get("doc") or {}).get("title") for x in hits]
            print(f"q={q!r:24} mode={d.get('mode'):8} n={len(hits)} {names}")


asyncio.run(main())
