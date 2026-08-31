"""诊断 UI 检索为空：用同一个 token 直连 REST，看是 UI 请求问题还是数据问题。

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/diag_ui.py
"""
import asyncio
import json
import os

import httpx

BASE = os.environ.get("MEM_TEST_BASE", "https://repo.xlingo.fun").rstrip("/")
TOKEN = open("/tmp/mem_ui_token.txt").read().strip()


async def main():
    h = {"Authorization": "Bearer " + TOKEN}
    async with httpx.AsyncClient(timeout=30, headers=h) as c:
        r = await c.get(BASE + "/api/v1/auth/me")
        print("me:", r.status_code, r.text[:200])

        r = await c.get(BASE + "/api/v1/documents", params={"limit": 20})
        print("documents:", r.status_code)
        try:
            d = r.json()
            print("  keys:", list(d.keys()))
            items = d.get("items") or d.get("documents") or []
            print("  total:", d.get("total"), "len:", len(items))
            for x in items:
                print("   -", x.get("id"), x.get("title"), "|", x.get("project"))
        except Exception as e:
            print("  parse fail:", e, r.text[:300])

        for q in ["xspeak 怎么发版", "xspeak", "发版", "记忆"]:
            r = await c.get(BASE + "/api/v1/search", params={"q": q, "limit": 5})
            try:
                d = r.json()
                hits = d.get("hits") or d.get("results") or []
                titles = [(x.get("doc") or {}).get("title") or x.get("title") for x in hits]
                print(f"search[{q}]: {r.status_code} keys={list(d.keys())} n={len(hits)} {titles}")
            except Exception:
                print(f"search[{q}]: {r.status_code} {r.text[:200]}")

        # UI 实际发的请求长什么样？把可能的参数名都试一遍
        for params in [{"query": "xspeak", "limit": 5}, {"q": "xspeak", "library": "main", "limit": 5}]:
            r = await c.get(BASE + "/api/v1/search", params=params)
            print("param-probe", params, "->", r.status_code, r.text[:200])


asyncio.run(main())
