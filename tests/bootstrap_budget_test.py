"""bootstrap 的 token 预算必须真的生效。

这里锁的是曾经的真实 bug：预算填 100 或 100000，返回的都是同一个
estimated_tokens（3430）。根因是超预算时用 `continue` 跳过当前文档去看下一篇，
配合 `and picked` 的短路，首篇永远无条件全量收录。

跑法：cd /opt/memorys && .venv/bin/python tests/bootstrap_budget_test.py
"""
import pathlib
import sys
import time

import httpx

KEY = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
BASE = "https://repo.xlingo.fun"
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}
TAG = str(int(time.time()))
PROJ = f"budgettest{TAG}"
FAIL = []


def ck(name, cond, detail: object = ""):
    if cond:
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name}  {detail}")


def main():
    with httpx.Client(base_url=BASE, timeout=90, headers=H) as c:
        # 造 6 篇已知大小的文档：单篇正文 ~1000 字符 ≈ 850 token
        ids = []
        for i in range(6):
            r = c.post("/api/v1/documents", json={
                "title": f"预算测试{i}-{TAG}", "type": "fact", "project": PROJ,
                "importance": 5 - (i % 3),
                "content": f"# 第{i}篇\n" + ("内容填充" * 250)})
            if r.status_code == 200:
                ids.append(r.json()["id"])
        ck("造了 6 篇测试文档", len(ids) == 6, len(ids))

        def boot(b):
            return c.get("/api/v1/bootstrap",
                         params={"project": PROJ, "token_budget": b}).json()

        # --- 核心：估算值绝不能超预算 ---
        rows = []
        for b in [200, 500, 1000, 2000, 4000, 8000, 50000]:
            d = boot(b)
            rows.append((b, d["token_budget"], d["document_count"], d["estimated_tokens"]))
            ck(f"预算 {b}：估算 {d['estimated_tokens']} 不超预算 {d['token_budget']}",
               d["estimated_tokens"] <= d["token_budget"],
               f"超了 {d['estimated_tokens'] - d['token_budget']}")

        # --- 预算不同，结果必须不同（这正是原 bug 的表征）---
        est = [r[3] for r in rows]
        ck("不同预算给出不同的估算值（不再恒定）", len(set(est)) > 1, est)

        # --- 单调性：预算越大，收的篇数不减、估算不减 ---
        cnt = [r[2] for r in rows]
        ck("篇数随预算单调不减", all(cnt[i] <= cnt[i+1] for i in range(len(cnt)-1)), cnt)
        ck("估算随预算单调不减", all(est[i] <= est[i+1] for i in range(len(est)-1)), est)

        # --- 小预算必须真的小 ---
        small = boot(300)
        ck("预算 300 时估算 <= 300", small["estimated_tokens"] <= 300, small["estimated_tokens"])
        ck("小预算下内容被截断", any(x["truncated"] for x in small["documents"]),
           [x["truncated"] for x in small["documents"]])
        ck("小预算至少返回 1 篇（别给空包）", small["document_count"] >= 1)

        # --- 大预算能收全 ---
        big = boot(200000)
        ck("大预算收全 6 篇", big["document_count"] == 6,
           f"{big['document_count']}/{big['total_documents']}")
        ck("大预算下不再截断", not any(x["truncated"] for x in big["documents"]))

        # --- digest 也要守预算（它是给 agent 直接用的）---
        for b in [300, 1000, 4000]:
            d = boot(b)
            dtok = max(1, int(len(d["digest"]) * 0.85))
            ck(f"预算 {b}：digest 估算 {dtok} 不超预算", dtok <= b, dtok)

        # --- 排序：高优先级类型必须先进包，不能被"塞缝隙"打乱 ---
        c.post("/api/v1/documents", json={
            "title": f"预算-决策-{TAG}", "type": "decision", "project": PROJ,
            "importance": 5, "content": "决策内容" * 100})
        c.post("/api/v1/documents", json={
            "title": f"预算-总结-{TAG}", "type": "project_summary", "project": PROJ,
            "importance": 5, "content": "总结内容" * 100})
        d = boot(1500)
        types = [x["type"] for x in d["documents"]]
        ck("小预算下优先收 project_summary（排序未被预算逻辑打乱）",
           types and types[0] == "project_summary", types)

        # --- 边界 ---
        ck("预算 0 被夹到下限而非返回空包", boot(0)["document_count"] >= 1)
        ck("预算负数不崩", boot(-5)["token_budget"] >= 200)

        print("\n预算 → 篇数 / 估算：")
        for b, tb, n, e in rows:
            print(f"  {b:>6} → {n} 篇 / {e} tokens")

        # 收尾
        for i in c.get("/api/v1/documents", params={"project": PROJ, "limit": 50}).json()["items"]:
            c.request("DELETE", f"/api/v1/documents/{i['id']}")

    print(f"\nFAIL={len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("✅ bootstrap 预算全部通过")


if __name__ == "__main__":
    main()
