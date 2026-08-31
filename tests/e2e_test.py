"""全链路 e2e：直接造测试用户拿本地 JWT，跑完 REST 所有端点 + 用户隔离。

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/e2e_test.py
"""
import asyncio
import sys

import httpx

import os

# 默认打本机；MEM_TEST_BASE=https://repo.xlingo.fun 可对公网域名跑同一套断言
BASE = os.environ.get("MEM_TEST_BASE", "http://127.0.0.1:8649").rstrip("/")
FAIL = []


def check(name, cond, extra=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAIL.append(name)
    print(f"[{tag}] {name}{(' — ' + str(extra)) if extra else ''}")


async def mk_user(username: str, uid_hint: int):
    """直连 DB 造用户 + 签本地 JWT（不碰 ai.xlingo.fun 真实账号）。"""
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
        gitsvc.ensure_repo(settings.data_dir, u.id)
        token, _ = issue_local_jwt(u)
        return token, u.id


DOCS = [
    dict(title="xspeak 项目架构总结", type="project_summary", project="xspeak",
         tags=["nextjs", "deploy"], importance=5,
         content="# 架构\n\nxspeak 是 Next.js 教学应用，部署在 /opt/xspeak 端口 3022。生产域名 speak.xlingo.fun，测试站走 /xlearn 前缀。数据库用 PostgreSQL。\n\n# 发版流程\n\ntar 打包时排除 .env.local 和 .cache，传到生产后再构建。"),
    dict(title="TTS 梯队决策", type="decision", project="xspeak",
         tags=["tts", "kokoro"], importance=5,
         content="# 结论\n\n只有凌普 4 点的 cron 用自建 Kokoro，其余全部走云端 MiMo。自建 9-10 秒且偶发 502，MiMo 只要 3.3 秒。"),
    dict(title="用户偏好：部署先备份", type="preference", project="",
         tags=["habit"], importance=4,
         content="改别人的站点或配置之前先备份，变更只做新增一整段，方便整段删除回退。"),
    dict(title="PG 端口约定", type="fact", project="infra",
         tags=["postgres"], importance=3,
         content="本机 PostgreSQL 18 监听 54321，不是默认的 5432。宝塔装的原生 PG，不是 Docker。"),
]


async def reset(uids: list[int]):
    """清掉测试用户的历史数据，保证 e2e 可重复跑。"""
    import shutil
    from pathlib import Path

    from sqlalchemy import text as sqltext

    from app.config import settings
    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(sqltext(
            "DELETE FROM chunks WHERE document_id IN "
            "(SELECT id FROM documents WHERE user_id = ANY(:u))"), {"u": uids})
        await s.execute(sqltext("DELETE FROM documents WHERE user_id = ANY(:u)"), {"u": uids})
        await s.execute(sqltext("DELETE FROM api_keys WHERE user_id = ANY(:u)"), {"u": uids})
        await s.commit()
    for uid in uids:
        p = Path(settings.data_dir) / "users" / f"u{uid}"
        if p.exists():
            shutil.rmtree(p)


async def main():
    token, uid = await mk_user("memtest", 999001)
    token2, uid2 = await mk_user("memtest2", 999002)
    await reset([uid, uid2])
    from app import gitsvc
    from app.config import settings
    gitsvc.ensure_repo(settings.data_dir, uid)
    gitsvc.ensure_repo(settings.data_dir, uid2)

    async with httpx.AsyncClient(base_url=BASE, timeout=30,
                                 headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.get("/api/health")
        check("health", r.status_code == 200 and r.json().get("ok"))

        r = await c.get("/api/v1/auth/me")
        check("auth/me", r.status_code == 200 and r.json()["username"] == "memtest", r.text[:120])

        # ---- 创建 ----
        ids = []
        for d in DOCS:
            r = await c.post("/api/v1/documents", json=d)
            if r.status_code == 200:
                ids.append(r.json()["id"])
            else:
                check(f"create {d['title']}", False, r.text[:160])
        check("创建 4 篇文档", len(ids) == 4, f"ids={ids}")

        # ---- 重复检测 ----
        r = await c.post("/api/v1/documents", json=DOCS[0])
        check("重复标题返回 409", r.status_code == 409, r.text[:160])

        # ---- 中文检索 ----
        cases = [
            ("xspeak 怎么发版", "xspeak 项目架构总结"),
            ("TTS 用哪个方案", "TTS 梯队决策"),
            ("postgres 端口是多少", "PG 端口约定"),
            ("Kokoro", "TTS 梯队决策"),
            ("部署前要做什么", None),
        ]
        for q, want in cases:
            r = await c.get("/api/v1/search", params={"q": q, "limit": 5})
            hits = r.json().get("results", []) if r.status_code == 200 else []
            titles = [h["doc"]["title"] for h in hits]
            ok = bool(hits) and (want is None or want in titles)
            check(f"检索「{q}」", ok, f"{r.json().get('mode','?')} -> {titles[:3]}")

        # ---- bootstrap ----
        r = await c.get("/api/v1/bootstrap", params={"project": "xspeak", "token_budget": 1500})
        b = r.json() if r.status_code == 200 else {}
        check("bootstrap(xspeak)", r.status_code == 200 and b.get("document_count", 0) >= 2,
              f"{b.get('document_count')} 篇 / ~{b.get('estimated_tokens')} tokens")

        r = await c.get("/api/v1/bootstrap", params={"token_budget": 300})
        b2 = r.json() if r.status_code == 200 else {}
        check("bootstrap token 预算生效", b2.get("estimated_tokens", 99999) <= 400 or b2.get("document_count", 9) <= 2,
              f"~{b2.get('estimated_tokens')} tokens / {b2.get('document_count')} 篇")

        # ---- 更新 ----
        r = await c.patch(f"/api/v1/documents/{ids[0]}",
                          json={"content": DOCS[0]["content"] + "\n\n# 补充\n\n生产可直接发版，不必先过测试服。",
                                "importance": 5})
        check("更新文档", r.status_code == 200, r.text[:120])

        r = await c.get("/api/v1/search", params={"q": "生产可直接发版", "limit": 5})
        check("更新后立即可检索", any("xspeak" in h["doc"]["title"] for h in r.json().get("results", [])),
              r.json().get("mode"))

        # ---- 软删除 / 回收站 / 恢复 ----
        r = await c.delete(f"/api/v1/documents/{ids[3]}")
        check("软删除", r.status_code == 200, r.text[:120])
        r = await c.get("/api/v1/documents", params={"trash": "true"})
        check("回收站可见", any(i["id"] == ids[3] for i in r.json()["items"]))
        r = await c.get("/api/v1/search", params={"q": "postgres 端口"})
        check("删除后不再命中检索", not any(h["doc"]["id"] == ids[3] for h in r.json().get("results", [])))
        r = await c.post(f"/api/v1/documents/{ids[3]}/restore")
        check("恢复", r.status_code == 200, r.text[:120])
        r = await c.get("/api/v1/search", params={"q": "postgres 端口"})
        check("恢复后重新命中", any(h["doc"]["id"] == ids[3] for h in r.json().get("results", [])))

        # ---- git 历史 ----
        r = await c.get(f"/api/v1/documents/{ids[0]}/history")
        h = r.json() if r.status_code == 200 else []
        check("git 版本历史", isinstance(h, list) and len(h) >= 2, f"{len(h) if isinstance(h,list) else 0} 次提交")

        # ---- API Key ----
        r = await c.post("/api/v1/keys", json={"name": "e2e"})
        raw = r.json().get("key", "") if r.status_code == 200 else ""
        check("创建 API Key", raw.startswith("hk_"), raw[:12] + "…")

        # ---- 磁盘同步 ----
        r = await c.post("/api/v1/sync", json={"action": "reindex"})
        check("磁盘重建索引", r.status_code == 200, r.text[:160])

    # ---- API Key 独立鉴权 ----
    async with httpx.AsyncClient(base_url=BASE, timeout=30, headers={"X-Api-Key": raw}) as ck:
        r = await ck.get("/api/v1/auth/me")
        check("API Key 鉴权", r.status_code == 200 and r.json()["via"] == "api-key", r.text[:120])
        r = await ck.get("/api/v1/search", params={"q": "xspeak"})
        check("API Key 可检索", r.status_code == 200 and len(r.json()["results"]) > 0)

    # ---- 用户隔离 ----
    async with httpx.AsyncClient(base_url=BASE, timeout=30,
                                 headers={"Authorization": f"Bearer {token2}"}) as c2:
        r = await c2.get("/api/v1/documents")
        check("隔离：user2 看不到 user1 文档", r.json()["total"] == 0, f"total={r.json()['total']}")
        r = await c2.get(f"/api/v1/documents/{ids[0]}")
        check("隔离：user2 取 user1 文档 404", r.status_code == 404, r.status_code)
        r = await c2.get("/api/v1/search", params={"q": "xspeak"})
        check("隔离：user2 检索为空", len(r.json()["results"]) == 0)

    # ---- 未认证 ----
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as anon:
        r = await anon.get("/api/v1/documents")
        check("未认证 401", r.status_code == 401, r.status_code)

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：{FAIL}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
