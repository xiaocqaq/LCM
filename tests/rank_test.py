"""排序质量：长度归一化 + importance 加权必须生效。

锁的是真实问题：RRF 只看排名不看体量，一篇 15303 字符的长文曾出现在
17 个查询里的 13 个、7 次排第一，把 194 字符的对题短文挤掉。

跑法：cd /opt/memorys && .venv/bin/python tests/rank_test.py
"""
import pathlib
import sys
import time

import httpx

KEY = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
BASE = "https://repo.xlingo.fun"
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}
TAG = str(int(time.time()))
PROJ = f"ranktest{TAG}"
FAIL = []


def ck(name, cond, detail: object = ""):
    if cond:
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name}  {detail}")


def main():
    with httpx.Client(base_url=BASE, timeout=90, headers=H) as c:
        ids = {}

        # 短文：只讲一件事，"熔断降级"是它的核心
        r = c.post("/api/v1/documents", json={
            "title": f"短文-熔断降级决策-{TAG}", "type": "decision", "project": PROJ,
            "importance": 5,
            "content": "# 结论\n网关启用熔断降级，阈值 50% 错误率。\n因为下游偶发抖动会拖垮整条链路。"})
        ids["short"] = r.json()["id"]

        # 长文：内容庞杂，也提到熔断但只是顺带一句。体量是短文的 ~40 倍。
        filler = "".join(
            f"\n## 第{i}节\n这一节讲部署、监控、日志、告警、容量、压测的琐碎细节。" * 3
            for i in range(30))
        r = c.post("/api/v1/documents", json={
            "title": f"长文-系统运维大全-{TAG}", "type": "howto", "project": PROJ,
            "importance": 3,
            "content": "# 运维手册\n" + filler + "\n## 附录\n另外网关也配了熔断降级。\n"})
        ids["long"] = r.json()["id"]

        # 同长度、同内容主题，只有 importance 不同 → 用来单独验 importance 加权
        for imp in (5, 1):
            r = c.post("/api/v1/documents", json={
                "title": f"同长度-灰度发布-imp{imp}-{TAG}", "type": "fact",
                "project": PROJ, "importance": imp,
                "content": "# 灰度发布\n先放 5% 流量观察十分钟，指标正常再全量。" * 3})
            ids[f"imp{imp}"] = r.json()["id"]

        def search(q):
            r = c.get("/api/v1/search", params={"q": q, "project": PROJ, "limit": 8})
            out, seen = [], set()
            for h in r.json().get("results", []):
                did = h["doc"]["id"]
                if did not in seen:
                    seen.add(did)
                    out.append(did)
            return out

        # --- 核心：短文对题时不该被长文压掉 ---
        order = search("熔断降级")
        ck("查「熔断降级」两篇都召回",
           ids["short"] in order and ids["long"] in order, order)
        si = order.index(ids["short"]) if ids["short"] in order else 99
        li = order.index(ids["long"]) if ids["long"] in order else 99
        ck(f"对题短文排在长文之前（短={si} 长={li}）", si < li,
           f"order={order} short={ids['short']} long={ids['long']}")
        ck("对题短文排第一", order and order[0] == ids["short"], order)

        # --- importance 加权：同长度同主题，高 importance 靠前 ---
        order2 = search("灰度发布 流量")
        hi = order2.index(ids["imp5"]) if ids["imp5"] in order2 else 99
        lo = order2.index(ids["imp1"]) if ids["imp1"] in order2 else 99
        ck(f"同长度时 importance 5 排在 importance 1 之前（{hi} vs {lo}）", hi < lo,
           f"order={order2}")

        # --- 长文不该被赶出结果：它只是不该靠体量刷榜 ---
        order3 = search("监控 告警 容量")
        ck("长文在自己真正对题的查询上仍能召回", ids["long"] in order3, order3)

        # --- 不能过度惩罚：长文独占某主题时应排第一 ---
        ck("长文独占主题时排第一", order3 and order3[0] == ids["long"], order3)

        print("\n收尾清理…")
        for i in c.get("/api/v1/documents",
                       params={"project": PROJ, "limit": 50}).json()["items"]:
            c.request("DELETE", f"/api/v1/documents/{i['id']}")

    print(f"\nFAIL={len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("✅ 排序质量全部通过")


if __name__ == "__main__":
    main()
