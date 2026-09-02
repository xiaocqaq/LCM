#!/usr/bin/env python3
"""开箱验收：以真实用户身份走一遍完整闭环，不打内部函数只打公网接口。

回答的是"现在能正常用了吗"，所以刻意只用外部可见的东西：
公网 HTTPS + API Key，跟 agent/浏览器看到的完全一致。
"""
import json
import pathlib
import sys
import time

import httpx

BASE = "https://repo.xlingo.fun"
KEY = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}
TAG = str(int(time.time()))
FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def mcp(c, tool, args):
    """按 MCP streamable HTTP 调一个工具，返回解析后的 payload。"""
    r = c.post("/mcp", headers={**H, "Accept": "application/json, text/event-stream"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": tool, "arguments": args}})
    body = r.text
    # streamable HTTP 会以 SSE 帧返回，取 data: 行
    for line in body.splitlines():
        if line.startswith("data:"):
            body = line[5:].strip()
            break
    d = json.loads(body)
    txt = d["result"]["content"][0]["text"]
    return json.loads(txt)


def main():
    with httpx.Client(base_url=BASE, timeout=90, headers=H, follow_redirects=True) as c:
        print("=" * 70)
        print("1. 服务在线")
        print("=" * 70)
        ck("首页可访问", c.get("/").status_code == 200)
        ck("健康检查", c.get("/api/health").json().get("ok") is True)
        ck("API 文档", c.get("/api/docs").status_code == 200)

        print()
        print("=" * 70)
        print("2. 鉴权（无 key 必须拒绝）")
        print("=" * 70)
        r = httpx.get(f"{BASE}/api/v1/documents", timeout=30)
        ck("无 key 访问被拒", r.status_code == 401, f"got {r.status_code}")
        r = httpx.get(f"{BASE}/api/v1/documents", timeout=30,
                      headers={"X-Api-Key": "hk_deadbeef_not_a_real_key"})
        ck("错误 key 被拒", r.status_code == 401, f"got {r.status_code}")

        print()
        print("=" * 70)
        print("3. REST：写入 → 检索 → 读取 → 历史")
        print("=" * 70)
        title = f"验收-向量检索接入记录-{TAG}"
        r = c.post("/api/v1/documents", json={
            "title": title, "type": "decision", "project": f"acc{TAG}",
            "importance": 5, "tags": ["向量", "检索"],
            "content": "# 结论\n接入阿里云百炼做语义补召回。\n"
                       "关键词检索对纯概念提问无能，比如问「数据存在哪里」时"
                       "正文写的是 Markdown 是 source of truth，一个词都对不上。"})
        ck("新建文档", r.status_code == 200, r.text[:120])
        doc_id = r.json().get("id")

        r = c.get("/api/v1/search", params={"q": "为什么要上语义检索", "limit": 5})
        hits = r.json().get("results", [])
        ck("语义检索命中刚写的文档",
           any(h["doc"]["id"] == doc_id for h in hits),
           [h["doc"]["title"][:24] for h in hits])
        ck("返回 hybrid 模式", r.json().get("mode") == "hybrid", r.json().get("mode"))

        r = c.get(f"/api/v1/documents/{doc_id}")
        ck("按 id 读全文", "source of truth" in r.json().get("content", ""))

        r = c.get(f"/api/v1/documents/{doc_id}/history")
        ck("git 历史有记录", isinstance(r.json(), list) and len(r.json()) >= 1)

        print()
        print("=" * 70)
        print("4. bootstrap：换 agent 后恢复上下文")
        print("=" * 70)
        r = c.get("/api/v1/bootstrap", params={"token_budget": 3000})
        b = r.json()
        ck("返回上下文包", b.get("document_count", 0) > 0, b.get("document_count"))
        ck("不超预算", b.get("estimated_tokens", 99999) <= 3000,
           f"{b.get('estimated_tokens')} > 3000")
        ck("有摘要", bool(b.get("digest")))
        print(f"     {b['document_count']}/{b['total_documents']} 篇，"
              f"{b['estimated_tokens']} tokens")

        print()
        print("=" * 70)
        print("5. MCP：agent 实际会走的路径")
        print("=" * 70)
        d = mcp(c, "memory_search", {"query": "语义检索为什么必要", "limit": 3})
        ck("memory_search 可用", d.get("ok") is True, str(d)[:100])
        ck("MCP 也走 hybrid 融合", d.get("mode") == "hybrid", d.get("mode"))
        ck("MCP 检索命中",
           any(x["doc"]["id"] == doc_id for x in d.get("results", [])),
           [x["doc"]["title"][:22] for x in d.get("results", [])])

        d = mcp(c, "memory_bootstrap", {"token_budget": 2000})
        ck("memory_bootstrap 可用", d.get("document_count", 0) > 0)

        d = mcp(c, "memory_list_docs", {"limit": 50})
        ck("memory_list_docs 可用", d.get("total", 0) > 0, str(d)[:80])

        print()
        print("=" * 70)
        print("6. 向量覆盖与降级")
        print("=" * 70)
        r = c.get("/api/v1/search", params={"q": "熔断", "limit": 3, "mode": "keyword"})
        ck("mode=keyword 可显式跳过向量", r.status_code == 200 and
           r.json().get("mode") == "keyword")

        print()
        print("=" * 70)
        print("7. 收尾：删除 → 回收站可恢复")
        print("=" * 70)
        r = c.delete(f"/api/v1/documents/{doc_id}")
        ck("软删除", r.status_code == 200, r.text[:100])
        r = c.get("/api/v1/search", params={"q": "为什么要上语义检索", "limit": 5})
        still = [h["doc"]["id"] for h in r.json().get("results", [])]
        ck("删除后检索不到", doc_id not in still, still)
        r = c.post(f"/api/v1/documents/{doc_id}/restore")
        ck("可从回收站恢复", r.status_code == 200, r.text[:100])
        c.delete(f"/api/v1/documents/{doc_id}")
        print("     已再次删除，库恢复原状")

    print()
    print("=" * 70)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过：{FAIL}")
        sys.exit(1)
    print("✅ 开箱验收全部通过 —— 可以正常使用")
    print("=" * 70)


main()
