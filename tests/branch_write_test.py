"""写入指定分支：不落盘、不进 DB、不动当前分支，切过去才可见。

核心断言是"隔离性"——写别的分支绝不能污染当前分支的工作区和索引。

跑法：cd /opt/memorys && .venv/bin/python tests/branch_write_test.py
"""
import pathlib
import subprocess
import sys
import time

import httpx

KEY = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
BASE = "https://repo.xlingo.fun"
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}
REPO = pathlib.Path("/var/lib/memorys/data/users/u20")
TAG = str(int(time.time()))
BR = f"wtest-{TAG}"
TITLE = f"分支写入验证 {TAG}"
FAIL = []


def ck(name, cond, detail: object = ""):
    if cond:
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name}  {detail}")


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, timeout=30).stdout.strip()


def main():
    with httpx.Client(base_url=BASE, timeout=90, headers=H) as c:
        cur = c.get("/api/v1/branches").json()["current"]
        print(f"当前分支：{cur}\n")

        # --- 写到一个不存在的分支 ---
        r = c.post("/api/v1/documents", params={"branch": BR}, json={
            "title": TITLE, "type": "fact", "project": "wtest",
            "content": "# 草稿\n这篇只应该存在于分支上。"})
        ck("写入新分支 200", r.status_code == 200, r.text[:150])
        d = r.json()
        ck("返回 ok", d.get("ok") is True, d)
        ck("自动创建了分支", d.get("created_branch") is True, d)
        ck("action=create", d.get("action") == "create", d)
        ck("返回了 commit hash", bool(d.get("commit")), d)
        ck("当前分支没变", d.get("current_branch") == cur, d)

        # --- 隔离性：这是这套功能的关键 ---
        ck("工作区依然干净（没被写脏）", git("status", "--porcelain") == "",
           git("status", "--porcelain"))
        ck("HEAD 还在原分支", git("rev-parse", "--abbrev-ref", "HEAD") == cur)

        titles = [i["title"] for i in c.get("/api/v1/documents",
                                            params={"limit": 60}).json()["items"]]
        ck("DB 里查不到（没进索引）", TITLE not in titles)

        # 断言"结果里没有这一篇"，不是"零命中"——标题里的「验证」「写入」
        # 是通用词，别的文档会正常命中，那是对的（同类错误踩过两次了）
        r = c.get("/api/v1/search", params={"q": TITLE, "limit": 10})
        hit_titles = [h["doc"]["title"] for h in r.json().get("results", [])]
        ck("检索结果里没有这一篇", TITLE not in hit_titles, hit_titles)

        ck("磁盘上没有这个文件",
           not list((REPO / "main").glob(f"*{TAG}*")),
           list((REPO / 'main').glob(f'*{TAG}*')))

        # --- 但它确实在那个分支的 git 历史里 ---
        ls = git("ls-tree", "-r", "--name-only", f"refs/heads/{BR}")
        ck("目标分支的 tree 里有这个文件", TAG in ls, ls[:200])
        blob = git("show", f"refs/heads/{BR}:{d['rel_path']}")
        ck("文件内容正确", "这篇只应该存在于分支上" in blob, blob[:120])
        ck("frontmatter 完整", "title:" in blob and "importance:" in blob, blob[:200])

        # --- 再写一次同标题 → 应该是 update 而非 create ---
        r2 = c.post("/api/v1/documents", params={"branch": BR}, json={
            "title": TITLE, "type": "fact", "project": "wtest",
            "content": "# 草稿 v2\n改过一版。"})
        d2 = r2.json()
        ck("同路径再写 → action=update", d2.get("action") == "update", d2)
        ck("不再报新建分支", d2.get("created_branch") is False, d2)
        blob2 = git("show", f"refs/heads/{BR}:{d['rel_path']}")
        ck("内容已更新", "改过一版" in blob2, blob2[:120])

        # --- 已有文档另存到分支（PATCH + branch）---
        base_title = f"另存基线 {TAG}"
        rb = c.post("/api/v1/documents", json={
            "title": base_title, "type": "fact", "content": "原始内容", "project": "wtest"})
        base_id = rb.json()["id"]
        r3 = c.patch(f"/api/v1/documents/{base_id}", params={"branch": BR},
                     json={"content": "分支上的改法"})
        ck("已有文档另存到分支 200", r3.status_code == 200, r3.text[:150])
        d3 = r3.json()
        ck("另存返回 ok", d3.get("ok") is True, d3)
        orig = c.get(f"/api/v1/documents/{base_id}").json()
        ck("原文档在当前分支未被改动", orig["content"] == "原始内容", orig["content"][:60])

        # --- 边界 ---
        r = c.post("/api/v1/documents", params={"branch": cur},
                   json={"title": f"x{TAG}", "content": "y"})
        ck("目标分支=当前分支 → 400", r.status_code == 400, r.text[:120])

        r = c.post("/api/v1/documents", params={"branch": "--evil"},
                   json={"title": f"x{TAG}", "content": "y"})
        ck("非法分支名 → 400", r.status_code == 400, r.text[:120])

        r = c.post("/api/v1/documents", params={"branch": BR},
                   json={"title": f"空内容{TAG}", "content": ""})
        ck("空正文 → 400", r.status_code == 400, r.text[:120])

        # --- 切过去之后应该能看到 ---
        c.post("/api/v1/branches/switch", json={"name": BR})
        c.post("/api/v1/sync", json={"action": "reindex"})
        titles = [i["title"] for i in c.get("/api/v1/documents",
                                            params={"limit": 60}).json()["items"]]
        ck("切到该分支后 DB 里能看到了", TITLE in titles, titles[:6])

        # --- 收尾：切回去，删分支和基线文档 ---
        c.post("/api/v1/branches/switch", json={"name": cur})
        c.post("/api/v1/sync", json={"action": "reindex"})
        c.request("DELETE", f"/api/v1/branches/{BR}", params={"force": "true"})
        c.request("DELETE", f"/api/v1/documents/{base_id}")
        ck("已切回原分支", c.get("/api/v1/branches").json()["current"] == cur)

    print(f"\nFAIL={len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("✅ 分支写入全部通过")


if __name__ == "__main__":
    main()
