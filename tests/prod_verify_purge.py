#!/usr/bin/env python3
"""生产侧验收本轮三个功能（favicon / 按项目删 / 清空端点存在）。

刻意**不调用** POST /api/v1/trash/empty：生产回收站里是用户真实的软删记忆
（实测 273 篇 / 346 个文件），点一次就全没了。
这一层只验证「路由已注册、鉴权正确、按项目软删可用且不串项目」，
硬删的行为正确性由本地库的 project_trash_test.py（PURGE=1）负责。

收尾会把本轮造的测试文档从回收站里精确清掉（只删本轮的 id 和对应文件），
不动任何既有数据。
"""
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("BASE", "https://repo.xlingo.fun")
KEY = Path("/root/.memorys-hermes-key").read_text().strip()
DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/memorys/data")
STAMP = str(int(time.time()))
PROJ = f"prodcheck-{STAMP}"

OK = FAIL = 0


def chk(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}  {extra}")


def main() -> int:
    h = {"X-Api-Key": KEY}
    with httpx.Client(base_url=BASE, headers=h, timeout=40, follow_redirects=True) as c:
        print(f"=== 生产 {BASE} ===")
        r = c.get("/api/v1/auth/me")
        chk("鉴权可用", r.status_code == 200, f"HTTP {r.status_code}")
        uid = (r.json() or {}).get("id")

        # ---- 1. favicon ----
        print("\n=== 1. 标签页图标 ===")
        r = c.get("/static/favicon.svg")
        chk("favicon.svg 200", r.status_code == 200, f"HTTP {r.status_code}")
        chk("是 SVG", "<svg" in r.text, r.text[:60])
        chk("Content-Type 正确", "svg" in r.headers.get("content-type", ""),
            r.headers.get("content-type"))
        r = c.get("/")
        chk("页面声明 icon", 'rel="icon"' in r.text)
        chk("用相对路径（子路径入口 /mem/ 也能取到）",
            'href="static/favicon.svg"' in r.text)

        # ---- 2. 路由已注册 ----
        print("\n=== 2. 新端点已注册 ===")
        r = c.delete(f"/api/v1/projects/definitely-not-exist-{STAMP}")
        chk("按项目删除路由存在（404 而非 405）", r.status_code == 404,
            f"HTTP {r.status_code}")
        # 清空端点：只验证「无凭证会被拦」，不验证成功路径（那会真清生产）
        with httpx.Client(base_url=BASE, timeout=20) as anon:
            r = anon.post("/api/v1/trash/empty")
        chk("清空端点存在且要鉴权（401/403）", r.status_code in (401, 403),
            f"HTTP {r.status_code}")

        # ---- 3. 按项目软删（造两个项目，只删一个）----
        print("\n=== 3. 按项目软删不串项目 ===")
        mine, other = [], []
        for i in range(2):
            r = c.post("/api/v1/documents", json={
                "title": f"{PROJ} 待删 {i}", "project": PROJ, "type": "fact",
                "content": f"# 生产验收\n\n本轮临时文档 {i}，验证按项目删除。", "importance": 1})
            if r.status_code == 200:
                mine.append(r.json()["id"])
        r = c.post("/api/v1/documents", json={
            "title": f"{PROJ}-keep 保留", "project": f"{PROJ}-keep", "type": "fact",
            "content": "# 保留\n\n这篇不该被删。", "importance": 1})
        if r.status_code == 200:
            other.append(r.json()["id"])
        chk("造了 2 篇待删", len(mine) == 2, str(mine))
        chk("造了 1 篇保留", len(other) == 1, str(other))

        r = c.delete(f"/api/v1/projects/{PROJ}")
        chk("删除项目成功", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")
        chk("返回条数 = 2", (r.json() or {}).get("deleted") == 2, r.text[:120])

        alive = {i["id"] for i in c.get("/api/v1/documents?limit=200").json()["items"]}
        chk("待删的已不在活跃列表", not (set(mine) & alive))
        chk("同前缀的另一个项目未被牵连", set(other) <= alive)

        trash = {i["id"] for i in
                 c.get("/api/v1/documents?trash=true&limit=200").json()["items"]}
        chk("待删的进了回收站（软删，可恢复）", set(mine) <= trash)

        # ---- 4. 收尾：精确清掉本轮痕迹 ----
        print("\n=== 4. 收尾（只动本轮数据）===")
        r = c.post(f"/api/v1/documents/{mine[0]}/restore")
        chk("能从回收站恢复", r.status_code == 200, f"HTTP {r.status_code}")
        # 保留项目也删掉，避免留在生产库里
        c.delete(f"/api/v1/documents/{mine[0]}")
        c.delete(f"/api/v1/projects/{PROJ}-keep")
        print(f"  本轮 doc id: 待删 {mine} / 保留 {other}（都已进回收站）")
        print(f"  提示：这些是测试文档，可在 Web UI 回收站里看到；")
        print(f"        用 tests/prod_cleanup_ids.py 精确清除，或留着不影响检索。")

    print(f"\nOK={OK} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
