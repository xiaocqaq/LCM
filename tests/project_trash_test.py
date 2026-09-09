#!/usr/bin/env python3
"""按项目删除 + 清空回收站 + favicon 的端到端测试。

只打 HTTP，不碰内部函数 —— 这三个都是"用户点一下会发生什么"的功能，
绕过路由层去调 service 的话，路径参数编码、哨兵值、鉴权这些最容易错的地方
恰好全都测不到。

跑法（默认打本地 SQLite 实例）：
    BASE=http://127.0.0.1:8650 python tests/project_trash_test.py
打生产（需要 key）：
    BASE=https://repo.xlingo.fun KEY=hk_xxx python tests/project_trash_test.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8650").rstrip("/")
KEY = os.environ.get("KEY", "").strip()
# 数据目录：用来核实 md 文件真的被移动/删除，而不是只改了数据库
DATA_DIR = os.environ.get("DATA_DIR", "").strip()

OK = FAIL = 0
STAMP = str(int(time.time()))
# 两个项目：一个用来删，一个用来证明"删 A 不会碰 B"
PROJ_A = f"ptest-a-{STAMP}"
PROJ_B = f"ptest-b-{STAMP}"


def chk(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}  {extra}")


def client() -> httpx.Client:
    h = {"X-Api-Key": KEY} if KEY else {}
    return httpx.Client(base_url=BASE, timeout=60, headers=h)


def mk(c: httpx.Client, proj: str, n: int) -> list[int]:
    ids = []
    for i in range(n):
        r = c.post("/api/v1/documents", json={
            "title": f"{proj} 第{i + 1}篇 {STAMP}",
            "content": f"# 小节{i + 1}\n\n这是 {proj} 的第 {i + 1} 篇测试记忆，用于验证按项目删除。\n",
            "type": "fact", "project": proj, "importance": 3,
        })
        if r.status_code == 200:
            ids.append(r.json()["id"])
    return ids


def trash_ids(c: httpx.Client) -> set[int]:
    r = c.get("/api/v1/documents", params={"trash": "true", "limit": 200})
    return {i["id"] for i in r.json().get("items", [])}


def alive_ids(c: httpx.Client) -> set[int]:
    r = c.get("/api/v1/documents", params={"limit": 200})
    return {i["id"] for i in r.json().get("items", [])}


def md_files() -> list[Path]:
    if not DATA_DIR:
        return []
    return sorted(Path(DATA_DIR).rglob("*.md"))


def trash_files() -> list[Path]:
    if not DATA_DIR:
        return []
    out = []
    for t in Path(DATA_DIR).rglob(".trash"):
        out += [f for f in t.iterdir() if f.is_file()]
    return sorted(out)


def main() -> int:
    print(f"=== 目标 {BASE}（{'带 key' if KEY else '免鉴权'}）===")
    with client() as c:
        r = c.get("/api/v1/auth/me")
        chk("鉴权可用", r.status_code == 200, f"HTTP {r.status_code} {r.text[:100]}")
        if r.status_code != 200:
            return 1

        # ---------- 1. favicon ----------
        print("\n=== 1. 标签页图标 ===")
        r = c.get("/static/favicon.svg")
        chk("favicon.svg 可访问", r.status_code == 200, f"HTTP {r.status_code}")
        chk("是 SVG 内容", "<svg" in r.text, r.text[:60])
        ct = r.headers.get("content-type", "")
        chk("Content-Type 是 svg", "svg" in ct, ct)
        r = c.get("/")
        html = r.text
        chk("页面声明了 icon", 'rel="icon"' in html)
        # 相对路径是关键：utils.xlingo.fun/mem/ 那个入口靠它才能取到图标
        chk("icon 用相对路径（非 /favicon.ico）",
            'href="static/favicon.svg"' in html and 'href="/favicon.ico"' not in html)
        chk("有 data URI 兜底", "data:image/svg+xml" in html)

        # ---------- 2. 造数据 ----------
        print("\n=== 2. 造两个项目 ===")
        a = mk(c, PROJ_A, 3)
        b = mk(c, PROJ_B, 2)
        chk("项目 A 建了 3 篇", len(a) == 3, str(a))
        chk("项目 B 建了 2 篇", len(b) == 2, str(b))
        if len(a) != 3 or len(b) != 2:
            return 1
        before_trash = trash_ids(c)

        # ---------- 3. 按项目删除 ----------
        print("\n=== 3. 删除整个项目 A ===")
        r = c.delete(f"/api/v1/projects/{PROJ_A}")
        chk("删除请求成功", r.status_code == 200, f"HTTP {r.status_code} {r.text[:140]}")
        body = r.json() if r.status_code == 200 else {}
        chk("返回删除条数 = 3", body.get("deleted") == 3, str(body)[:140])

        alive = alive_ids(c)
        chk("A 的 3 篇都不在活跃列表里", not (set(a) & alive), str(set(a) & alive))
        chk("B 的 2 篇不受影响", set(b) <= alive, f"缺 {set(b) - alive}")

        tr = trash_ids(c)
        chk("A 的 3 篇进了回收站", set(a) <= tr, f"缺 {set(a) - tr}")
        chk("B 没被牵连进回收站", not (set(b) & tr), str(set(b) & tr))

        # 检索也不该再命中
        r = c.get("/api/v1/search", params={"q": f"{PROJ_A} 测试记忆", "limit": 10})
        hits = {h["doc"]["id"] for h in r.json().get("results", [])}
        chk("删除后检索不再命中 A", not (set(a) & hits), str(set(a) & hits))

        # ---------- 4. 恢复一篇（证明是软删除）----------
        print("\n=== 4. 软删除可恢复 ===")
        r = c.post(f"/api/v1/documents/{a[0]}/restore")
        chk("能从回收站恢复", r.status_code == 200, r.text[:120])
        chk("恢复后回到活跃列表", a[0] in alive_ids(c))
        # 再删回去，方便后面测清空
        c.delete(f"/api/v1/documents/{a[0]}")

        # ---------- 5. 不存在的项目 ----------
        print("\n=== 5. 边界情况 ===")
        r = c.delete(f"/api/v1/projects/nope-{STAMP}")
        chk("删不存在的项目返回 404", r.status_code == 404, f"HTTP {r.status_code}")
        # 未归项目的哨兵值：至少要能被路由到（有没有内容取决于库里的数据）
        r = c.delete("/api/v1/projects/__none__")
        chk("__none__ 哨兵能被路由（200 或 404，不是 405/422）",
            r.status_code in (200, 404), f"HTTP {r.status_code} {r.text[:100]}")

        # ---------- 6. 清空回收站 ----------
        print("\n=== 6. 清空回收站（硬删）===")
        # 生产库回收站里是用户真实数据（实测 273 篇 / 346 个文件），
        # 对着生产跑这一步等于把用户所有软删记忆一次性抹掉。
        # 必须显式 PURGE=1 才执行；默认跳过并给出原因，而不是"看起来通过"。
        if os.environ.get("PURGE") != "1":
            print("  SKIP  未设 PURGE=1，跳过硬删（避免清掉非本轮写入的回收站）")
        else:
            n_before = len(trash_ids(c))
            files_before = len(trash_files())
            chk("清空前回收站非空", n_before > 0, f"{n_before} 篇")
            r = c.post("/api/v1/trash/empty")
            chk("清空请求成功", r.status_code == 200, f"HTTP {r.status_code} {r.text[:140]}")
            res = r.json() if r.status_code == 200 else {}
            chk("返回清掉的条数", res.get("purged", 0) >= n_before,
                f"purged={res.get('purged')} 期望≥{n_before}")

            after = trash_ids(c)
            chk("回收站已空", not after, f"还剩 {len(after)} 篇")
            chk("A 的文档彻底消失（连回收站也没有）", not (set(a) & after))
            # 真删要确认 404 而不是仍能取到
            r = c.get(f"/api/v1/documents/{a[1]}")
            chk("硬删后详情返回 404", r.status_code == 404, f"HTTP {r.status_code}")
            chk("B 仍然完好", set(b) <= alive_ids(c))

            if DATA_DIR:
                chk(".trash 目录已清空", not trash_files(),
                    f"还剩 {len(trash_files())} 个文件（清空前 {files_before}）")
                names = {p.name for p in md_files()}
                chk("A 的 md 不在工作区", not any(PROJ_A in n for n in names))
                chk("B 的 md 还在工作区", any(PROJ_B in n for n in names))
            else:
                print("  （未给 DATA_DIR，跳过文件系统核实）")

            # ---------- 7. 清空空回收站要幂等 ----------
            r = c.post("/api/v1/trash/empty")
            chk("再清一次不报错（幂等）", r.status_code == 200, f"HTTP {r.status_code}")
            chk("第二次 purged=0", (r.json() or {}).get("purged") == 0, r.text[:100])

        # ---------- 收尾 ----------
        # B 一定要软删掉，别在库里留活跃的测试文档。
        # 但硬删（purge）只在 PURGE=1 时做：默认跑法下回收站里可能还有
        # 用户自己的真实数据，收尾不该顺手把它们一起抹了。
        c.delete(f"/api/v1/projects/{PROJ_B}")
        if os.environ.get("PURGE") == "1":
            c.post("/api/v1/trash/empty")
        else:
            print("  （收尾：B 已软删进回收站；未 purge，可自行恢复或清空）")

    print(f"\nOK={OK} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
