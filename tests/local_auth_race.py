#!/usr/bin/env python3
"""复现 local 模式下偶发 401 的并发竞态。

现象：同一个 URL、同一份配置，`hermes mcp add` 有时 Connected、有时 401。
怀疑点是 local_context() 的"没有就现开一个用户"这段在并发下会撞
xiaoai_user_id 的 unique 约束，异常被 mcp_app 统一翻成 401。

用法：
    python tests/local_auth_race.py <base_url> [并发数] [轮数]
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8650"
CONC = int(sys.argv[2]) if len(sys.argv) > 2 else 12
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 6

REQ = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "race", "version": "1"}},
}
HDR = {"content-type": "application/json",
       "accept": "application/json, text/event-stream"}


async def one(c: httpx.AsyncClient) -> int:
    try:
        r = await c.post(f"{BASE}/mcp/", headers=HDR, json=REQ, timeout=30)
        return r.status_code
    except Exception:
        return -1


async def main() -> int:
    codes: dict[int, int] = {}
    bodies: list[str] = []
    async with httpx.AsyncClient() as c:
        for rd in range(ROUNDS):
            res = await asyncio.gather(*[one(c) for _ in range(CONC)])
            for code in res:
                codes[code] = codes.get(code, 0) + 1
            # 抓一个失败样本的响应体
            if any(x == 401 for x in res) and not bodies:
                r = await c.post(f"{BASE}/mcp/", headers=HDR, json=REQ, timeout=30)
                bodies.append(f"{r.status_code} {r.text[:300]}")
            print(f"  轮 {rd + 1}: {sorted(res)}")

    total = sum(codes.values())
    print(f"\n并发 {CONC} × {ROUNDS} 轮 = {total} 个请求")
    for k in sorted(codes):
        label = {200: "200 成功", 401: "401 认证失败", -1: "连接异常"}.get(k, str(k))
        print(f"  {label}: {codes[k]}")
    if bodies:
        print("\n失败样本：")
        for b in bodies:
            print("  ", b)
    bad = total - codes.get(200, 0)
    print(f"\n结论：{'有竞态，' + str(bad) + ' 个请求失败' if bad else '未复现，全部 200'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
