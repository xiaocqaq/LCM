"""公网 MCP 实测：经 nginx 反代（https://utils.xlingo.fun/mem/mcp）跑完整 MCP 握手 + 工具调用。

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/public_mcp_test.py <API_KEY>
"""
import asyncio
import json
import sys

import httpx

URL = "https://utils.xlingo.fun/mem/mcp"
FAIL = []


def check(name, cond, extra=""):
    if not cond:
        FAIL.append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{(' — ' + str(extra)[:110]) if extra else ''}")


def parse(r):
    """streamable HTTP 可能回 SSE（text/event-stream）也可能回纯 JSON。"""
    ct = r.headers.get("content-type", "")
    if "event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None
    try:
        return r.json()
    except Exception:
        return None


def text_of(res):
    try:
        return "".join(c.get("text", "") for c in res["result"]["content"])
    except Exception:
        return json.dumps(res, ensure_ascii=False)[:200]


async def main():
    key = sys.argv[1]
    h = {
        "X-Api-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    n = [0]

    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as c:
        async def rpc(method, params=None):
            n[0] += 1
            r = await c.post(URL, headers=h, json={
                "jsonrpc": "2.0", "id": n[0], "method": method, "params": params or {}})
            return r.status_code, parse(r)

        st, res = await rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "public-test", "version": "1.0"},
        })
        srv = (res or {}).get("result", {}).get("serverInfo", {})
        check("公网 initialize", st == 200 and srv.get("name") == "memorys", f"{st} {srv}")

        st, res = await rpc("tools/list")
        names = [t["name"] for t in (res or {}).get("result", {}).get("tools", [])]
        check("公网 tools/list", st == 200 and len(names) >= 9, f"{len(names)} 个工具")

        st, res = await rpc("tools/call", {
            "name": "memory_write",
            "arguments": {
                "title": "公网 MCP 链路验证",
                "content": "经 nginx 反代 https://utils.xlingo.fun/mem/mcp 调用 MCP 工具写入成功。验证 DNS rebinding 白名单与 SSE 反代配置生效。",
                "type": "fact",
                "project": "memorys",
                "tags": ["mcp", "nginx"],
            }})
        t = text_of(res or {})
        check("公网 memory_write", st == 200 and "已写入" in t, t[:90])

        st, res = await rpc("tools/call", {
            "name": "memory_search",
            "arguments": {"query": "公网 反代 验证", "limit": 5}})
        t = text_of(res or {})
        check("公网 memory_search", st == 200 and "命中" in t, t.split("\n")[0][:90])

        st, res = await rpc("tools/call", {
            "name": "memory_bootstrap",
            "arguments": {"project": "memorys", "token_budget": 2000}})
        t = text_of(res or {})
        check("公网 memory_bootstrap", st == 200 and "上下文引导包" in t, t.split("\n")[0][:90])

    # 未认证必须 401
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as anon:
        r = await anon.post(URL, headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        check("公网未认证 401", r.status_code == 401, r.status_code)

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：{FAIL}")
        sys.exit(1)
    print("✅ 公网 MCP 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
