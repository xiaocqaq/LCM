"""MCP streamable HTTP e2e：走真实 MCP 协议，验证 9 个工具 + 鉴权。

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/mcp_test.py
"""
import asyncio
import sys

import httpx

BASE = "http://127.0.0.1:8649"
MCP = BASE + "/mcp/"
FAIL = []


def check(name, cond, extra=""):
    if not cond:
        FAIL.append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")


async def mk_token():
    from sqlalchemy import select

    from app import gitsvc
    from app.config import settings
    from app.db import SessionLocal
    from app.models import User
    from app.security import issue_local_jwt

    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.username == "memtest"))).scalar_one_or_none()
        if not u:
            u = User(username="memtest", display_name="memtest", xiaoai_user_id=999001,
                     email="memtest@example.local", role="user")
            s.add(u)
            await s.commit()
        gitsvc.ensure_repo(settings.data_dir, u.id)
        tok, _ = issue_local_jwt(u)
        return tok


def parse_sse(body: str):
    """streamable HTTP 用 SSE 回包，取出 data: 行的 JSON。"""
    import json
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


class McpClient:
    def __init__(self, token: str):
        self.h = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.sid = None
        self.n = 0

    async def call(self, c, method, params=None, notify=False):
        self.n += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self.n
        h = dict(self.h)
        if self.sid:
            h["mcp-session-id"] = self.sid
        r = await c.post(MCP, json=payload, headers=h)
        if "mcp-session-id" in r.headers:
            self.sid = r.headers["mcp-session-id"]
        if notify:
            return r.status_code, None
        return r.status_code, parse_sse(r.text) or r.text


def tool_text(res) -> str:
    try:
        return res["result"]["content"][0]["text"]
    except Exception:
        return str(res)[:300]


async def main():
    token = await mk_token()
    cli = McpClient(token)

    async with httpx.AsyncClient(timeout=60) as c:
        st, res = await cli.call(c, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "1.0"},
        })
        check("initialize", st == 200 and "result" in res,
              res.get("result", {}).get("serverInfo") if isinstance(res, dict) else res)
        await cli.call(c, "notifications/initialized", {}, notify=True)

        st, res = await cli.call(c, "tools/list")
        names = [t["name"] for t in res.get("result", {}).get("tools", [])] if isinstance(res, dict) else []
        want = {"memory_search", "memory_get", "memory_write", "memory_update", "memory_delete",
                "memory_bootstrap", "memory_list_libraries", "memory_list_docs", "memory_history"}
        check("tools/list 9 个工具", want.issubset(set(names)), f"{len(names)} 个: {names}")

        async def tool(name, args):
            return await cli.call(c, "tools/call", {"name": name, "arguments": args})

        st, res = await tool("memory_write", {
            "title": "MCP 跨会话验证",
            "content": "这条记忆通过 MCP 协议写入，用来验证换 agent 后能否读回。",
            "type": "fact", "project": "memorys", "tags": ["mcp", "e2e"], "importance": 4,
        })
        txt = tool_text(res)
        check("memory_write", st == 200 and ("已写入" in txt or "已存在" in txt), txt[:100])

        st, res = await tool("memory_search", {"query": "跨会话 验证", "limit": 5})
        txt = tool_text(res)
        check("memory_search 命中", st == 200 and "MCP 跨会话验证" in txt, txt.splitlines()[0][:100] if txt else "")

        st, res = await tool("memory_list_docs", {"limit": 10})
        txt = tool_text(res)
        check("memory_list_docs", st == 200 and "MCP 跨会话验证" in txt, txt.splitlines()[0][:80])

        st, res = await tool("memory_bootstrap", {"project": "memorys", "token_budget": 1000})
        txt = tool_text(res)
        check("memory_bootstrap", st == 200 and "MCP 跨会话验证" in txt, txt.splitlines()[0][:80])

        st, res = await tool("memory_list_libraries", {})
        check("memory_list_libraries", st == 200 and "main" in tool_text(res), tool_text(res)[:80])

        # 从 search 结果里抓 doc id 做 get/update/history/delete
        import re
        m = re.search(r"doc=(\d+)", tool_text((await tool("memory_search", {"query": "跨会话", "limit": 3}))[1]))
        did = int(m.group(1)) if m else None
        check("拿到 doc id", did is not None, did)

        if did:
            st, res = await tool("memory_get", {"doc_id": did})
            check("memory_get 全文", st == 200 and "MCP 协议写入" in tool_text(res), tool_text(res)[:80])

            st, res = await tool("memory_update", {"doc_id": did, "content": "更新后的正文：MCP 链路验证通过。", "importance": 5})
            check("memory_update", st == 200 and "已更新" in tool_text(res), tool_text(res)[:80])

            st, res = await tool("memory_history", {"doc_id": did})
            txt = tool_text(res)
            # 期望格式：- <日期> <短 hash> <message>，至少两条（create + update）
            import re as _re
            commits = _re.findall(r"^- .+ [0-9a-f]{7,} .+$", txt, _re.M)
            check("memory_history", st == 200 and len(commits) >= 2, f"{len(commits)} 次提交")

            st, res = await tool("memory_delete", {"doc_id": did})
            check("memory_delete", st == 200 and ("已删除" in tool_text(res) or "回收站" in tool_text(res)), tool_text(res)[:80])

    # 未认证必须 401
    async with httpx.AsyncClient(timeout=30) as anon:
        r = await anon.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "x", "version": "1"}}},
                            headers={"Content-Type": "application/json",
                                     "Accept": "application/json, text/event-stream"})
        check("MCP 未认证 401", r.status_code == 401, r.status_code)

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：{FAIL}")
        sys.exit(1)
    print("✅ MCP 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
