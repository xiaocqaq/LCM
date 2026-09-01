"""分支管理端到端：建 → 切 → 写 → 合 → 删，含非法输入与边界。

跑法：cd /opt/memorys && .venv/bin/python tests/branch_test.py
"""
import pathlib
import sys
import time

import httpx

KEY = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
BASE = "https://repo.xlingo.fun"
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}
FAIL = []
TAG = str(int(time.time()))
BR = f"test-branch-{TAG}"


def ck(name, cond, detail: object = ""):
    if cond:
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name}  {detail}")


def main():
    with httpx.Client(base_url=BASE, timeout=90, headers=H) as c:
        # 环境准备：确保有至少一次提交，否则分支操作无从下手
        c.post("/api/v1/documents", json={
            "title": f"分支测试基线 {TAG}", "type": "fact",
            "content": "基线内容", "project": "branchtest"})

        d = c.get("/api/v1/branches").json()
        main_br = d["current"]
        ck("列出分支，标出当前分支", any(b["current"] for b in d["branches"]), d)
        ck("返回 dirty 状态", "dirty" in d)

        # --- 非法分支名必须被挡（这些值会拼进 git 命令）---
        for bad, why in [("--force", "像 git flag"),
                         ("a..b", "含 .."),
                         ("has space", "含空格"),
                         ("x@{1}", "含 @{"),
                         ("trail/", "以 / 结尾"),
                         ("", "空名")]:
            r = c.post("/api/v1/branches", json={"name": bad})
            ck(f"拒绝非法分支名 {bad!r}（{why}）", r.status_code == 400,
               f"got {r.status_code}")

        # --- 建分支 ---
        r = c.post("/api/v1/branches", json={"name": BR, "switch": True})
        ck("创建分支 200", r.status_code == 200, r.text[:120])
        ck("创建后已切换过去", r.json().get("current") == BR, r.json())

        r = c.post("/api/v1/branches", json={"name": BR})
        ck("重复创建同名 → 400", r.status_code == 400, r.text[:100])

        # --- 在分支上写内容 ---
        t = f"只存在于分支的记忆 {TAG}"
        r = c.post("/api/v1/documents", json={
            "title": t, "type": "fact", "content": "分支专属内容", "project": "branchtest"})
        ck("分支上可写入记忆", r.status_code == 200, r.text[:120])
        doc_id = r.json().get("id")

        # --- 切回主分支：分支上写的文件应该从磁盘消失 ---
        r = c.post("/api/v1/branches/switch", json={"name": main_br})
        ck("切回主分支 200", r.status_code == 200, r.text[:120])
        ck("current 已变回主分支", r.json().get("current") == main_br, r.json())

        c.post("/api/v1/sync", json={"action": "reindex"})
        titles = [i["title"] for i in c.get("/api/v1/documents",
                                            params={"limit": 50}).json()["items"]]
        ck("主分支上看不到分支专属记忆", t not in titles,
           f"意外出现：{t}")

        # --- 合并回来 ---
        r = c.post("/api/v1/branches/merge", json={"name": BR})
        ck("合并 200", r.status_code == 200, r.text[:120])
        ck("合并结果 ok", r.json().get("ok") is True, r.json())

        c.post("/api/v1/sync", json={"action": "reindex"})
        titles = [i["title"] for i in c.get("/api/v1/documents",
                                            params={"limit": 50}).json()["items"]]
        ck("合并后主分支能看到该记忆", t in titles, titles[:5])

        # --- 边界 ---
        r = c.post("/api/v1/branches/merge", json={"name": main_br})
        ck("不能把分支合并到自己 → 400", r.status_code == 400, r.text[:100])

        r = c.post("/api/v1/branches/switch", json={"name": f"nope-{TAG}"})
        ck("切到不存在的分支 → 400", r.status_code == 400, r.text[:100])

        r = c.request("DELETE", f"/api/v1/branches/{main_br}")
        ck("不能删当前所在分支 → 400", r.status_code == 400, r.text[:100])

        # --- 删除（已合并，普通删除即可成功）---
        r = c.request("DELETE", f"/api/v1/branches/{BR}")
        ck("删除已合并分支成功", r.status_code == 200 and r.json().get("ok"),
           r.text[:150])
        names = [b["name"] for b in c.get("/api/v1/branches").json()["branches"]]
        ck("分支已不在列表里", BR not in names, names)

        # --- 收尾：删掉测试文档 ---
        if doc_id:
            c.request("DELETE", f"/api/v1/documents/{doc_id}")

    print(f"\nFAIL={len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("✅ 分支管理全部通过")


if __name__ == "__main__":
    main()
