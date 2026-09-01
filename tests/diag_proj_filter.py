#!/usr/bin/env python3
"""本地诊断通过、公网 rank_test 失败，同一份代码。差别是 rank_test 带 project 过滤。
验证向量路在 project 过滤下是否行为不同。
"""
import asyncio, time

from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import Document, User
from app import service as SV

TAG = str(int(time.time()))
PROJ = f"diagproj{TAG}"


async def main():
    async with SessionLocal() as s:
        u = (await s.execute(select(User).limit(1))).scalars().first()

        short_body = "# 结论\n网关启用熔断降级，阈值 50% 错误率。\n因为下游偶发抖动会拖垮整条链路。"
        filler = "".join(
            f"\n## 第{i}节\n这一节讲部署、监控、日志、告警、容量、压测的琐碎细节。" * 3
            for i in range(30))
        long_body = "# 运维手册\n" + filler + "\n## 附录\n另外网关也配了熔断降级。\n"

        ids = {}
        for key, title, body, imp, ty in (
            ("short", f"短文-熔断降级决策-{TAG}", short_body, 5, "decision"),
            ("long", f"长文-系统运维大全-{TAG}", long_body, 3, "howto"),
        ):
            d = await SV.create_document(s, u, {
                "title": title, "content": body, "type": ty,
                "project": PROJ, "importance": imp})
            ids[key] = d.id
        await s.commit()
        print(f"short doc={ids['short']} ({len(short_body)}字符 imp=5)")
        print(f"long  doc={ids['long']} ({len(long_body)}字符 imp=3)  project={PROJ}\n")

        q = "熔断降级"
        for label, kw in (("不带 project 过滤", {}), ("带 project 过滤", {"project": PROJ})):
            print("=" * 62)
            print(f"{label}   query「{q}」")
            print("=" * 62)
            sem = await SV.semantic_search(s, u, q, limit=20, **kw)
            print(f"  向量路: {'None' if sem is None else f'{len(sem)} 条'}")
            if sem:
                for i, x in enumerate(sem[:4]):
                    print(f"    {i+1}. sim={x['score']:.4f} doc={x['document_id']} "
                          f"{x['doc']['title'][:26]}")
            r = await SV.search_chunks(s, u, q, limit=10, **kw)
            print(f"  融合后 {len(r)} 条:")
            for i, x in enumerate(r[:5]):
                print(f"    {i+1}. score={x['score']:.6f} doc={x['doc']['id']} "
                      f"imp={x['doc']['importance']} {x['doc']['title'][:26]}")
            sp = next((i for i, x in enumerate(r) if x["doc"]["id"] == ids["short"]), None)
            lp = next((i for i, x in enumerate(r) if x["doc"]["id"] == ids["long"]), None)
            ok = sp is not None and (lp is None or sp < lp)
            print(f"  → short={sp} long={lp} {'✅' if ok else '❌ 长文在前'}\n")

        for did in ids.values():
            d = (await s.execute(select(Document).where(Document.id == did))).scalars().first()
            if d:
                await SV.soft_delete_document(s, u, d)
        await s.commit()
        print("已清理")

    await engine.dispose()


asyncio.run(main())
