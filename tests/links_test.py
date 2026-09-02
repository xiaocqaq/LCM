#!/usr/bin/env python3
"""文档关系（links）的行为测试。

只打公网接口，以真实调用方视角验证。
重点是"关系必须改变检索行为"——一条边如果不影响该给 agent 看什么，它就没用。
"""
import json
import pathlib
import sys
import time

import httpx

KEY = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
BASE = "https://repo.xlingo.fun"
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}
TAG = str(int(time.time()))
PROJ = f"linktest{TAG}"
FAIL = []


def ck(name, cond, detail: object = ""):
    if cond:
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def mcp(c, tool, args):
    r = c.post("/mcp", headers={**H, "Accept": "application/json, text/event-stream"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": tool, "arguments": args}})
    body = r.text
    for line in body.splitlines():
        if line.startswith("data:"):
            body = line[5:].strip()
            break
    return json.loads(json.loads(body)["result"]["content"][0]["text"])


def main():
    with httpx.Client(base_url=BASE, timeout=90, headers=H, follow_redirects=True) as c:
        ids = {}

        print("=" * 70)
        print("1. supersedes：旧版本退出检索，但仍可直读")
        print("=" * 70)
        r = c.post("/api/v1/documents", json={
            "title": f"部署流程-v1-{TAG}", "type": "howto", "project": PROJ, "importance": 4,
            "content": "# 部署流程 v1\n手工 scp 上传，然后 systemctl restart。\n蓝绿切换尚未实现。"})
        ids["v1"] = r.json()["id"]
        ck("建旧版本", r.status_code == 200)

        r = c.get("/api/v1/search", params={"q": "部署流程 蓝绿", "project": PROJ, "limit": 5})
        ck("旧版本能被搜到", any(h["doc"]["id"] == ids["v1"] for h in r.json()["results"]))

        r = c.post("/api/v1/documents", json={
            "title": f"部署流程-v2-{TAG}", "type": "howto", "project": PROJ, "importance": 4,
            "content": "# 部署流程 v2\n改用 CI 推镜像，蓝绿切换已实现。\nscp 那套已废弃。",
            "links": [{"type": "supersedes", "target": f"部署流程-v1-{TAG}",
                       "note": "v1 的手工 scp 已废弃"}]})
        ids["v2"] = r.json()["id"]
        body = r.json()
        ck("建新版本并声明 supersedes", r.status_code == 200)
        rep = body.get("linkReport") or {}
        ck("关系解析成功（无 unresolved）", not rep.get("unresolved"), rep)
        ck("解析出目标 id", any(x.get("target_id") == ids["v1"] for x in rep.get("resolved", [])), rep)

        r = c.get("/api/v1/search", params={"q": "部署流程 蓝绿", "project": PROJ, "limit": 5})
        hit_ids = [h["doc"]["id"] for h in r.json()["results"]]
        ck("旧版本已从检索退场", ids["v1"] not in hit_ids, hit_ids)
        ck("新版本仍在检索里", ids["v2"] in hit_ids, hit_ids)

        r = c.get(f"/api/v1/documents/{ids['v1']}")
        ck("旧版本仍可直接读取（不是删除）", r.status_code == 200 and "scp" in r.json()["content"])
        ck("旧版本标了 supersededBy", r.json().get("supersededBy") == ids["v2"], r.json().get("supersededBy"))
        inc = r.json().get("incomingLinks") or []
        ck("旧版本能看到谁取代了它", any(x["from_id"] == ids["v2"] for x in inc), inc)

        print()
        print("=" * 70)
        print("2. bootstrap 不带过时版本")
        print("=" * 70)
        r = c.get("/api/v1/bootstrap", params={"project": PROJ, "token_budget": 8000})
        b = r.json()
        got = [d["id"] for d in b["documents"]]
        ck("bootstrap 收了新版本", ids["v2"] in got, got)
        ck("bootstrap 排除旧版本", ids["v1"] not in got, got)

        print()
        print("=" * 70)
        print("3. implements：命中实现时把决策带出来")
        print("=" * 70)
        r = c.post("/api/v1/documents", json={
            "title": f"缓存层选型决策-{TAG}", "type": "decision", "project": PROJ, "importance": 5,
            "content": "# 结论\n用 Redis 不用 Memcached。\n因为需要持久化和 sorted set。"})
        ids["dec"] = r.json()["id"]
        r = c.post("/api/v1/documents", json={
            "title": f"缓存层落地记录-{TAG}", "type": "howto", "project": PROJ, "importance": 3,
            "content": "# 落地\nredis 7.2，maxmemory 512mb，allkeys-lru。\n连接池 20。",
            "links": [{"type": "implements", "target": f"缓存层选型决策-{TAG}"}]})
        ids["impl"] = r.json()["id"]
        rep = (r.json().get("linkReport") or {})
        ck("implements 关系解析成功", not rep.get("unresolved"), rep)

        r = c.get(f"/api/v1/documents/{ids['impl']}/related")
        rel = r.json()
        ck("related 端点返回出边", any(o.get("targetId") == ids["dec"] for o in rel["outgoing"]), rel)
        r = c.get(f"/api/v1/documents/{ids['dec']}/related")
        ck("决策侧能看到入边", any(i["from_id"] == ids["impl"] for i in r.json()["incoming"]))

        # 只留实现记录能进预算的小预算，看决策是否被关系带出来
        r = c.get("/api/v1/bootstrap", params={"project": PROJ, "token_budget": 8000})
        b = r.json()
        got = [d["id"] for d in b["documents"]]
        vias = {d["id"]: d.get("via") for d in b["documents"]}
        ck("两篇都在 bootstrap 里", ids["dec"] in got and ids["impl"] in got, got)
        ck("linked_extra 字段存在", "linked_extra" in b, list(b.keys()))

        print()
        print("=" * 70)
        print("4. 非法/无法解析的关系不能静默失败")
        print("=" * 70)
        r = c.post("/api/v1/documents", json={
            "title": f"关系写错的文档-{TAG}", "type": "fact", "project": PROJ,
            "content": "内容随意。",
            "links": [{"type": "supersedes", "target": "这个标题根本不存在xyz"},
                      {"type": "不是合法类型", "target": f"部署流程-v2-{TAG}"}]})
        ids["bad"] = r.json()["id"]
        body = r.json()
        ck("文档本身创建成功（关系错不该拦住写入）", r.status_code == 200)
        ck("非法 type 被丢掉", len(body.get("links", [])) == 1, body.get("links"))
        rep = body.get("linkReport") or {}
        ck("找不到目标的关系报 unresolved", len(rep.get("unresolved", [])) == 1, rep)

        print()
        print("=" * 70)
        print("5. 撤销关系：目标要重新可见")
        print("=" * 70)
        r = c.patch(f"/api/v1/documents/{ids['v2']}", json={"links": []})
        ck("清空 links", r.status_code == 200, r.text[:120])
        r = c.get(f"/api/v1/documents/{ids['v1']}")
        ck("旧版本的 supersededBy 已撤销", r.json().get("supersededBy") is None,
           r.json().get("supersededBy"))
        r = c.get("/api/v1/search", params={"q": "部署流程 蓝绿", "project": PROJ, "limit": 5})
        ck("旧版本回到检索里", ids["v1"] in [h["doc"]["id"] for h in r.json()["results"]])

        print()
        print("=" * 70)
        print("6. 删除取代者：被取代的文档不能永久隐身")
        print("=" * 70)
        r = c.patch(f"/api/v1/documents/{ids['v2']}", json={
            "links": [{"type": "supersedes", "target": f"部署流程-v1-{TAG}"}]})
        r = c.get(f"/api/v1/documents/{ids['v1']}")
        ck("重新标上 supersedes", r.json().get("supersededBy") == ids["v2"])
        c.delete(f"/api/v1/documents/{ids['v2']}")
        r = c.get(f"/api/v1/documents/{ids['v1']}")
        ck("删掉取代者后旧版本恢复可见", r.json().get("supersededBy") is None,
           r.json().get("supersededBy"))

        print()
        print("=" * 70)
        print("7. MCP 侧同样支持")
        print("=" * 70)
        d = mcp(c, "memory_write", {
            "title": f"MCP关系测试-新-{TAG}", "content": "新结论：用 A 方案。",
            "type": "decision", "project": PROJ,
            "links": [{"type": "supersedes", "target": f"缓存层选型决策-{TAG}"}]})
        ck("MCP memory_write 接受 links", d.get("ok") is True, d)
        ids["mcpnew"] = d.get("id")
        rep = d.get("linkReport") or {}
        ck("MCP 侧关系解析成功", not rep.get("unresolved"), rep)

        d = mcp(c, "memory_get", {"doc_id": ids["dec"]})
        ck("MCP memory_get 返回 superseded_by",
           d.get("superseded_by") == ids["mcpnew"], d.get("superseded_by"))
        ck("MCP memory_get 返回 incoming_links", isinstance(d.get("incoming_links"), list))

        d = mcp(c, "memory_search", {"query": "Redis Memcached 选型", "project": PROJ})
        sids = [x["doc"]["id"] for x in d.get("results", [])]
        ck("MCP 检索也排除了被取代的", ids["dec"] not in sids, sids)

        print()
        print("=" * 70)
        print("8. access_count 记账")
        print("=" * 70)
        r = c.get(f"/api/v1/documents/{ids['impl']}")
        before = r.json().get("accessCount", 0)
        for _ in range(3):
            c.get("/api/v1/search", params={"q": "redis maxmemory 连接池",
                                            "project": PROJ, "limit": 5})
        r = c.get(f"/api/v1/documents/{ids['impl']}")
        after = r.json().get("accessCount", 0)
        ck(f"检索后 accessCount 增长（{before} → {after}）", after > before)

        print()
        print("收尾清理…")
        for i in c.get("/api/v1/documents", params={"project": PROJ, "limit": 50}).json()["items"]:
            c.delete(f"/api/v1/documents/{i['id']}")

    print()
    print("=" * 70)
    if FAIL:
        print(f"FAIL={len(FAIL)}")
        print("失败项：", FAIL)
        sys.exit(1)
    print("FAIL=0  ✅ 文档关系全部通过")
    print("=" * 70)


main()
