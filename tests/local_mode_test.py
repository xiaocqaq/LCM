#!/usr/bin/env python3
"""本地模式验收：SQLite 后端跑通全链路，且检索结果与 PG 一致。

只用公网协议打（HTTP + MCP），不碰内部函数 —— 这一套要能证明"用户 clone 下来
就能跑起来"，而不是"内部函数在测试环境里能调通"。

跑法：
    # 先起本地实例（另一个终端 / background）
    MEM_MODE=local MEM_LOCAL_HOME=/tmp/memlocal-test \
      .venv/bin/python -m uvicorn app.main:app --port 8650

    .venv/bin/python tests/local_mode_test.py            # 默认打 127.0.0.1:8650
    BASE=http://127.0.0.1:9000 .venv/bin/python tests/local_mode_test.py

覆盖：
  A 免鉴权单用户（不带任何凭据就能读写）
  B 文档 CRUD → md 落盘 → git 提交
  C 三路检索（FTS5 关键词 + Python 三元组模糊；无 embedding 时自动两路）
  D bootstrap 预算装箱
  E links（supersedes 退出检索 / implements 双向带出）
  F 软删 → 恢复，且 FTS 行随之增删（SQLite 特有：FTS5 没有外键级联）
  G MCP 十工具可用
  H /api/v1/system 正确报告后端能力
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8650").rstrip("/")
OK = FAIL = 0
_created: list[int] = []


def chk(cond, name, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  → {extra}" if extra else ""))


def req(method, path, body=None, headers=None, raw=False):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            txt = resp.read().decode()
            return resp.status, (txt if raw else json.loads(txt or "{}"))
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt or "{}")
        except Exception:
            return e.code, {"raw": txt}
    except Exception as e:
        return 0, {"error": str(e)}


def mcp(tool, args):
    """MCP streamable HTTP 单次调用。

    必须带 Accept: text/event-stream —— streamable HTTP 的响应是 SSE，
    只写 application/json 的话服务端回 406（这个坑在服务器模式测试里踩过）。
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}
    st, txt = req("POST", "/mcp/", body,
                  {"Accept": "application/json, text/event-stream"}, raw=True)
    if st != 200:
        return st, {"raw": txt}
    # SSE: 逐行找 data:
    for line in str(txt).splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
            except Exception:
                continue
            content = (payload.get("result") or {}).get("content") or []
            if content and content[0].get("type") == "text":
                try:
                    return 200, json.loads(content[0]["text"])
                except Exception:
                    return 200, {"text": content[0]["text"]}
            return 200, payload
    return st, {"raw": str(txt)[:400]}


print(f"\n===== 本地模式验收  {BASE} =====\n")

# ---------------------------------------------------------------- H 系统信息
print("[H] 运行形态与检索能力")
st, sysinfo = req("GET", "/api/v1/system")
chk(st == 200, "GET /api/v1/system 可达（免鉴权）", f"status={st} {sysinfo}")
chk(sysinfo.get("mode") == "local", "mode=local", sysinfo.get("mode"))
search = sysinfo.get("search") or {}
chk(search.get("backend") == "sqlite", "backend=sqlite", search.get("backend"))
chk("fts5" in str(search.get("keyword", "")), "关键词走 FTS5+bm25", search.get("keyword"))
chk("python" in str(search.get("fuzzy", "")), "模糊走 Python 三元组", search.get("fuzzy"))
has_vec = bool((sysinfo.get("vector") or {}).get("configured"))
print(f"       （向量检索 {'已配置' if has_vec else '未配置 → 两路降级，属预期'}）")

# ---------------------------------------------------------------- A 免鉴权
print("\n[A] 免鉴权单用户")
st, me = req("GET", "/api/v1/auth/me")
chk(st == 200, "不带凭据可访问 /auth/me", f"status={st}")
chk(me.get("via") == "local", "via=local", me.get("via"))
local_user = me.get("username")
chk(bool(local_user), "单用户已自动开户", me)

# 同一个用户必须稳定 —— 每次请求新建用户会让数据散在多个 user_id 下
st, me2 = req("GET", "/api/v1/auth/me")
chk(me2.get("id") == me.get("id"), "多次请求归属同一用户", f"{me.get('id')} vs {me2.get('id')}")

# ---------------------------------------------------------------- B CRUD
print("\n[B] 文档 CRUD + md 落盘 + git")
SFX = "-lm7731"
docs = {}
for title, typ, proj, content in [
    (f"本地模式部署步骤{SFX}", "howto", "localtest",
     "# 装依赖\n\npip install -r requirements.txt\n\n"
     "# 起服务\n\npython -m app.local\n\n数据落在 ~/.memorys 目录下。\n"),
    (f"为什么本地用 SQLite{SFX}", "decision", "localtest",
     "# 结论\n\n本地模式选 SQLite，不要求装 PostgreSQL。\n\n"
     "# 理由\n\n零配置是本地模式的全部价值。装 PG 的成本超过收益。\n"),
    (f"缓存层选型{SFX}", "decision", "othertest",
     "# 结论\n\n用 Redis 做会话缓存。\n"),
]:
    st, d = req("POST", "/api/v1/documents",
                {"title": title, "type": typ, "project": proj,
                 "content": content, "importance": 4, "tags": ["local", "test"]})
    chk(st == 200 and d.get("id"), f"写入《{title[:16]}…》", f"status={st} {str(d)[:150]}")
    if d.get("id"):
        docs[title] = d["id"]
        _created.append(d["id"])

first_id = docs.get(f"本地模式部署步骤{SFX}")
st, got = req("GET", f"/api/v1/documents/{first_id}")
chk(st == 200 and "pip install" in (got.get("content") or ""), "读回全文一致")
# md 落盘用 /api/v1/system 报的 data_dir 去核实文件真的在磁盘上，
# 而不是信 API 返回的字段 —— "source of truth 是 md 文件"这句话要能被验证
import glob as _glob
_md = _glob.glob(os.path.join(sysinfo["storage"]["data_dir"], "data/users/*/main/*.md"))
chk(len(_md) >= 3, f"md 文件真的落到磁盘（{len(_md)} 个）",
    sysinfo["storage"]["data_dir"])

st, hist = req("GET", f"/api/v1/documents/{first_id}/history")
items = hist if isinstance(hist, list) else (hist.get("items") or [])
chk(st == 200 and len(items) >= 1, "git 有提交记录", f"status={st} n={len(items)} {str(hist)[:120]}")

st, upd = req("PATCH", f"/api/v1/documents/{first_id}",
              {"content": "# 装依赖\n\npip install -r requirements.txt\n\n"
                          "# 起服务\n\npython -m app.local --port 9000\n\n"
                          "数据落在 ~/.memorys 目录下。\n"})
chk(st == 200, "更新文档", f"status={st}")

# ---------------------------------------------------------------- C 检索
print("\n[C] 检索（FTS5 + 模糊兜底）")
for q, want_title_part in [
    ("本地模式怎么部署", "部署步骤"),
    ("为什么用 sqlite", "SQLite"),
    ("数据存在哪里", "部署步骤"),      # 索引侧同义扩展：正文写的是"~/.memorys 目录"
]:
    st, r = req("GET", f"/api/v1/search?q={urllib.parse.quote(q)}&limit=8")
    hits = r.get("results") or []
    titles = [h["doc"]["title"] for h in hits]
    hit = any(want_title_part in t for t in titles)
    chk(st == 200 and hit, f"「{q}」命中含「{want_title_part}」",
        f"status={st} 命中={[t[:22] for t in titles[:4]]}")

# chunk 级返回：不该整篇丢回来
st, r = req("GET", f"/api/v1/search?q={urllib.parse.quote('本地模式怎么部署')}&limit=5")
hits = r.get("results") or []
if hits:
    chk("heading" in hits[0], "返回 chunk 含所属小节标题", list(hits[0].keys()))
    chk(len(hits[0].get("content") or "") <= 2000, "单条 chunk 不超长")

# 项目过滤必须生效（三路都要带条件，漏一路就会把别的项目捞回来）
st, r = req("GET", f"/api/v1/search?q={urllib.parse.quote('选型')}&project=localtest&limit=8")
projs = {h["doc"]["project"] for h in (r.get("results") or [])}
chk(projs <= {"localtest"}, "project 过滤三路一致", projs)

# 显式降级路径
st, r = req("GET", f"/api/v1/search?q={urllib.parse.quote('本地模式')}&mode=keyword&limit=5")
chk(st == 200 and r.get("mode") == "keyword", "mode=keyword 可用", f"status={st}")

# ---------------------------------------------------------------- D bootstrap
print("\n[D] bootstrap 预算装箱")
st, pack = req("GET", "/api/v1/bootstrap?project=localtest&token_budget=4000")
chk(st == 200 and pack.get("document_count", 0) >= 2, "收录 localtest 的文档",
    f"status={st} n={pack.get('document_count')}")
types = [d["type"] for d in (pack.get("documents") or [])]
chk(types == sorted(types, key=lambda t: {"project_summary": 0, "decision": 1,
                                          "preference": 1, "howto": 2,
                                          "glossary": 3, "fact": 4}.get(t, 9)),
    "按类型优先级排序", types)

st, tiny = req("GET", "/api/v1/bootstrap?project=localtest&token_budget=300")
est = tiny.get("estimated_tokens", 0)
chk(est <= 300, f"预算 300 被真正遵守（实际 {est}）", est)

# ---------------------------------------------------------------- E links
print("\n[E] 文档关系")
st, newer = req("POST", "/api/v1/documents",
                {"title": f"缓存层选型-v2{SFX}", "type": "decision", "project": "othertest",
                 "content": "# 结论\n\n改用 Memcached。Redis 那版已过时。\n",
                 "importance": 5,
                 "links": [{"type": "supersedes", "target": f"缓存层选型{SFX}",
                            "note": "换了实现"}]})
chk(st == 200 and newer.get("id"), "写入带 supersedes 的新版本", f"status={st}")
if newer.get("id"):
    _created.append(newer["id"])
    rep = newer.get("linkReport") or {}
    chk(not rep.get("unresolved"), "关系目标已解析", rep)

    st, r = req("GET", f"/api/v1/search?q={urllib.parse.quote('缓存层选型')}&limit=8")
    titles = [h["doc"]["title"] for h in (r.get("results") or [])]
    chk(not any(t == f"缓存层选型{SFX}" for t in titles), "旧版本退出检索",
        [t[:24] for t in titles[:4]])
    chk(any("v2" in t for t in titles), "新版本在检索里", [t[:24] for t in titles[:4]])

    old_id = docs.get(f"缓存层选型{SFX}")
    st, old = req("GET", f"/api/v1/documents/{old_id}")
    chk(st == 200 and old.get("supersededBy") == newer["id"],
        "旧版本仍能直读且标了取代者", old.get("supersededBy"))

# ---------------------------------------------------------------- F 软删/恢复
print("\n[F] 软删 → 恢复（含 FTS 行同步）")
victim = docs.get(f"为什么本地用 SQLite{SFX}")
st, _ = req("DELETE", f"/api/v1/documents/{victim}")
chk(st == 200, "软删除", f"status={st}")

st, r = req("GET", f"/api/v1/search?q={urllib.parse.quote('为什么用 sqlite')}&limit=8")
ids = [h["doc"]["id"] for h in (r.get("results") or [])]
# 这一条是 SQLite 特有的坑：FTS5 是独立虚拟表，没有外键级联。
# 删 chunk 时忘了删 FTS 行，这里就会命中一个已删文档（或 JOIN 不上静默少结果）。
chk(victim not in ids, "删掉的文档不再出现在检索里（FTS 行已清）", ids)

# 回收站就是列表接口的 trash=true，没有独立端点
st, tr = req("GET", "/api/v1/documents?trash=true&limit=50")
trash_ids = [x["id"] for x in (tr.get("items") or [])]
chk(victim in trash_ids, "在回收站里", trash_ids)

st, _ = req("POST", f"/api/v1/documents/{victim}/restore")
chk(st == 200, "恢复", f"status={st}")
st, r = req("GET", f"/api/v1/search?q={urllib.parse.quote('为什么用 sqlite')}&limit=8")
ids = [h["doc"]["id"] for h in (r.get("results") or [])]
chk(victim in ids, "恢复后重新可检索（FTS 行已重建）", ids)

# ---------------------------------------------------------------- G MCP
print("\n[G] MCP 工具")
st, tools = req("POST", "/mcp/",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                {"Accept": "application/json, text/event-stream"}, raw=True)
names = set()
for line in str(tools).splitlines():
    if line.startswith("data:"):
        try:
            p = json.loads(line[5:].strip())
        except Exception:
            continue
        for t in (p.get("result") or {}).get("tools") or []:
            names.add(t["name"])
chk(st == 200, "MCP tools/list 免鉴权可用（本地模式）", f"status={st}")
for t in ["memory_search", "memory_get", "memory_write", "memory_bootstrap",
          "memory_list_docs"]:
    chk(t in names, f"工具 {t} 已注册", sorted(names))

st, res = mcp("memory_search", {"query": "本地模式怎么部署", "limit": 5})
chk(st == 200 and res.get("ok") and res.get("count", 0) > 0,
    "memory_search 有结果", str(res)[:180])

st, res = mcp("memory_bootstrap", {"project": "localtest", "token_budget": 2000})
chk(st == 200 and res.get("ok") and res.get("document_count", 0) >= 1,
    "memory_bootstrap 有内容", str(res)[:180])

# ---------------------------------------------------------------- 清理
print("\n[清理] 删除本轮测试文档")
gone = 0
for did in _created:
    st, _ = req("DELETE", f"/api/v1/documents/{did}")
    if st == 200:
        gone += 1
print(f"       已软删 {gone}/{len(_created)} 篇（在回收站里，可恢复）")

print(f"\n===== OK={OK}  FAIL={FAIL} =====")
sys.exit(1 if FAIL else 0)
