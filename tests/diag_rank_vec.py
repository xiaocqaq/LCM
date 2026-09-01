#!/usr/bin/env python3
"""为什么加了向量路之后 rank_test 的短文不再排第一。

假设：测试新建的文档没拿到向量（建的时候上游超时），于是三路里只有两路给它们打分，
而老文档有向量能拿额外票数 —— 长度归一化根本没机会起作用。
逐路打印验证，不猜。
"""
import asyncio, time

from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import Document, User
from app import service as SV

TAG = str(int(time.time()))
SHORT = f"短文-熔断降级-诊断{TAG}"
LONG = f"长文-系统运维-诊断{TAG}"


async def main():
    async with SessionLocal() as s:
        u = (await s.execute(select(User).limit(1))).scalars().first()

        short_body = "# 结论\n网关启用熔断降级，阈值 50% 错误率。\n因为下游偶发抖动会拖垮整条链路。"
        filler = "".join(
            f"\n## 第{i}节\n这一节讲部署、监控、日志、告警、容量、压测的琐碎细节。" * 3
            for i in range(30))
        long_body = "# 运维手册\n" + filler + "\n## 附录\n另外网关也配了熔断降级。\n"

        ids = {}
        for title, body, imp, ty in ((SHORT, short_body, 5, "decision"),
                                     (LONG, long_body, 3, "howto")):
            d = await SV.create_document(s, u, {
                "title": title, "content": body, "type": ty,
                "project": f"diag{TAG}", "importance": imp})
            ids[title] = d.id
        await s.commit()
        print(f"短文 doc={ids[SHORT]} {len(short_body)} 字符 importance=5")
        print(f"长文 doc={ids[LONG]} {len(long_body)} 字符 importance=3\n")

        print("=== 关键：新建的文档有向量吗 ===")
        for t, did in ids.items():
            rows = (await s.execute(text(
                "SELECT count(*), count(embedding) FROM chunks WHERE document_id=:i"
            ), {"i": did})).one()
            print(f"  {t[:22]}: {rows[0]} chunk，{rows[1]} 有向量 "
                  f"{'✅' if rows[1] == rows[0] else '❌ 缺向量 ← 这就是原因'}")

        q = "熔断降级"
        print(f"\n查询「{q}」\n")

        print("=== 向量路单独看（★ = 本次新建）===")
        sem = await SV.semantic_search(s, u, q, limit=20)
        if sem is None:
            print("  向量路返回 None（不可用或超时）")
        else:
            for i, x in enumerate(sem[:8]):
                mark = "★" if x["document_id"] in ids.values() else " "
                print(f"  {i+1:>2}. {mark} sim={x['score']:.4f} doc={x['document_id']} "
                      f"{x['doc']['title'][:32]}")

        print("\n=== 三路融合最终结果 ===")
        r = await SV.search_chunks(s, u, q, limit=10)
        for i, x in enumerate(r):
            did = x["doc"]["id"]
            mark = "★" if did in ids.values() else " "
            print(f"  {i+1:>2}. {mark} score={x['score']:.6f} doc={did} "
                  f"imp={x['doc']['importance']} {x['doc']['title'][:32]}")

        sp = next((i for i, x in enumerate(r) if x["doc"]["id"] == ids[SHORT]), None)
        lp = next((i for i, x in enumerate(r) if x["doc"]["id"] == ids[LONG]), None)
        print(f"\n短文位置 {sp}  长文位置 {lp}  "
              f"{'✅ 短文在前' if (sp is not None and (lp is None or sp < lp)) else '❌ 长文在前或短文没召回'}")

        for did in ids.values():
            d = (await s.execute(select(Document).where(Document.id == did))).scalars().first()
            if d:
                await SV.soft_delete_document(s, u, d)
        await s.commit()
        print("已清理")

    await engine.dispose()


asyncio.run(main())
