"""MCP streamable-HTTP 实测：鉴权、工具清单、write/search/bootstrap/list/history。

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/mcp_test.py
"""
import asyncio
import json

import httpx

import os

# 默认打本机；MEM_TEST_BASE=https://repo.xlingo.fun 可对公网域名跑同一套断言
BASE = os.environ.get("MEM_TEST_BASE", "http://127.0.0.1:8649").rstrip("/")
MCP = BASE + "/mcp"
FAIL = []


def check(name: str, cond, extra: object = ""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAIL.append(name)
    print(f"[{tag}] {name}{(' — ' + str(extra)) if extra else ''}")


def parse_sse(text: str):
    """streamable-http 的响应是 SSE，抽出第一个 data: 的 JSON。"""
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    # 也可能直接是 JSON
    try:
        return json.loads(text)
    except Exception:
        return None


async def mk_user(username: str, uid_hint: int):
    from app.db import SessionLocal
    from app.models import User
    from app.security import issue_local_jwt
    from app import gitsvc
    from app.config import settings
    from sqlalchemy import select

    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if not u:
            u = User(username=username, display_name=username, xiaoai_user_id=uid_hint,
                     email=f"{username}@example.local", role="user")
            s.add(u)
            await s.commit()
            await s.refresh(u)
        gitsvc.ensure_repo(settings.data_dir, u.id)
        token, _ = issue_local_jwt(u)
        return token, u.id


class McpClient:
    def __init__(self, token: str):
        self.h = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.sid = None
        self.n = 0

    async def call(self, client, method, params=None, notify=False):
        self.n += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = self.n
        if params is not None:
            body["params"] = params
        h = dict(self.h)
        if self.sid:
            h["mcp-session-id"] = self.sid
        r = await client.post(MCP, headers=h, json=body)
        if "mcp-session-id" in r.headers:
            self.sid = r.headers["mcp-session-id"]
        if notify:
            return r.status_code, None
        return r.status_code, parse_sse(r.text)

    async def tool(self, client, name, args):
        code, data = await self.call(client, "tools/call", {"name": name, "arguments": args})
        if not data or "result" not in data:
            return code, data, None
        content = data["result"].get("content") or []
        payload = None
        if content and content[0].get("type") == "text":
            try:
                payload = json.loads(content[0]["text"])
            except Exception:
                payload = content[0]["text"]
        return code, data, payload


async def main():
    token, uid = await mk_user("mcptest", 999003)
    print(f"user id={uid}")

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. 未认证
        r = await client.post(MCP, headers={"Content-Type": "application/json",
                                           "Accept": "application/json, text/event-stream"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                               "clientInfo": {"name": "t", "version": "1"}}})
        check("未认证 MCP 返回 401", r.status_code == 401, r.status_code)

        c = McpClient(token)

        # 2. initialize
        code, data = await c.call(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "1.0"}})
        check("initialize", code == 200 and data and "result" in data,
              (data or {}).get("result", {}).get("serverInfo"))
        await c.call(client, "notifications/initialized", notify=True)

        # 3. tools/list
        code, data = await c.call(client, "tools/list")
        names = [t["name"] for t in (data or {}).get("result", {}).get("tools", [])]
        want = {"memory_search", "memory_get", "memory_write", "memory_update",
                "memory_delete", "memory_bootstrap", "memory_list_libraries",
                "memory_list_docs", "memory_history"}
        check("tools/list 9 个工具", want.issubset(set(names)), names)

        # 4. memory_write
        code, _, w1 = await c.tool(client, "memory_write", {
            "title": "MCP 写入验证",
            "content": "# 验证\n\n这条记忆是通过 MCP streamable HTTP 写入的，用于验证 contextvar 鉴权链路。项目代号 memorys。",
            "type": "fact", "project": "memorys", "tags": ["mcp", "verify"], "importance": 4})
        doc_id = (w1 or {}).get("id") or (w1 or {}).get("documentId")
        check("memory_write", bool(doc_id), w1)

        code, _, w2 = await c.tool(client, "memory_write", {
            "title": "MCP 决策记录",
            "content": "# 决策\n\nMCP 鉴权放在 ASGI 层做，不在工具函数里，认证结果用 contextvars 传递。",
            "type": "decision", "project": "memorys", "tags": ["mcp"], "importance": 5})
        check("memory_write 第二篇", bool((w2 or {}).get("id")), w2)

        # 5. memory_search
        code, _, s1 = await c.tool(client, "memory_search", {"query": "MCP 鉴权怎么做的", "limit": 5})
        hits = (s1 or {}).get("hits") or (s1 or {}).get("results") or []
        check("memory_search 命中", len(hits) > 0,
              [(h.get("doc") or {}).get("title") for h in hits])

        # 6. memory_get
        code, _, g1 = await c.tool(client, "memory_get", {"doc_id": doc_id})
        check("memory_get", (g1 or {}).get("title") == "MCP 写入验证", (g1 or {}).get("title"))

        # 7. memory_update
        code, _, u1 = await c.tool(client, "memory_update", {
            "doc_id": doc_id, "content": "# 验证\n\n已通过 MCP 更新，追加了 pgvector 可选向量检索的说明。"})
        check("memory_update", bool(u1) and "error" not in str(u1)[:40], str(u1)[:80])

        # 8. memory_bootstrap
        code, _, b1 = await c.tool(client, "memory_bootstrap", {"project": "memorys", "token_budget": 2000})
        check("memory_bootstrap", bool((b1 or {}).get("documents") or (b1 or {}).get("docs")), str(b1)[:100])

        # 9. list_libraries / list_docs
        code, _, l1 = await c.tool(client, "memory_list_libraries", {})
        check("memory_list_libraries", bool(l1), str(l1)[:80])
        code, _, l2 = await c.tool(client, "memory_list_docs", {"limit": 10})
        check("memory_list_docs", bool(l2), str(l2)[:80])

        # 10. history
        code, _, h1 = await c.tool(client, "memory_history", {"doc_id": doc_id})
        check("memory_history", bool(h1), str(h1)[:100])

        # 11. delete
        code, _, d1 = await c.tool(client, "memory_delete", {"doc_id": doc_id})
        check("memory_delete", bool(d1), str(d1)[:80])

        # 12. 跨用户隔离：另一个用户看不到
        token2, uid2 = await mk_user("mcptest2", 999004)
        c2 = McpClient(token2)
        await c2.call(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "e2e2", "version": "1.0"}})
        await c2.call(client, "notifications/initialized", notify=True)
        code, _, s2 = await c2.tool(client, "memory_search", {"query": "MCP 鉴权怎么做的", "limit": 5})
        hits2 = (s2 or {}).get("hits") or (s2 or {}).get("results") or []
        check("隔离：user2 MCP 检索为空", len(hits2) == 0, hits2)

    print()
    if FAIL:
        print("❌ 失败项:", FAIL)
        raise SystemExit(1)
    print("✅ MCP 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
