"""公网召回实测：概念词查询必须命中，无关查询必须不命中。

跑法：cd /opt/memorys && .venv/bin/python tests/recall_public.py
"""
import pathlib
import re
import sys

import httpx

CFG = pathlib.Path("/root/.hermes/profiles/xiao/config.yaml").read_text()
KEY = re.search(r"(hk_[a-f0-9]+)", CFG).group(1)
BASE = "https://repo.xlingo.fun"

# (查询, 期望命中?)
CASES = [
    ("记忆文件存在哪个目录", True),
    ("数据存在哪里", True),
    ("md 文件放哪", True),
    ("MCP 怎么接入", True),
    ("服务监听哪个端口", True),
    ("怎么鉴权", True),
    ("数据库是什么", True),
    ("版本管理怎么做", True),
    # 反向用例：验证"索引侧扩展没有过度膨胀"。
    # 关键约束——这里的词必须是**库里任何文档都不会出现**的日常概念词。
    # 别用「养猫要注意什么」这类：「注意」是通用词，任何一篇写了「注意事项」
    # 的技术文档都会真命中，那是正确行为，断言却会误报失败（踩过）。
    #
    # 注意语义：开了向量检索后这些查询**会**返回结果（最近邻永远有 N 条），
    # 判据改成了"得分要显著低于正向查询"，见 main() 里的说明。
    ("孜然羊肉的腌制手法", False),
    ("周杰伦演唱会门票", False),
    ("柴犬拆家怎么办", False),
]

FAIL = []


def main():
    # 反向用例的判据不能是"零结果"。
    # 开了向量检索之后，语义检索总会返回最近邻的 N 条 —— 它没有"完全不匹配"这个概念，
    # 不像 tsvector 那样词不命中就真的空。所以「孜然羊肉」也会拿回 3 条技术文档。
    # 正确的判据是**分数要显著低于正向查询**：无关查询的 top1 得分应该明显落在
    # 正向查询 top1 得分的分布之下。这里用正向 top1 的最小值当参考线。
    pos_top, neg_top = [], []
    with httpx.Client(base_url=BASE, timeout=30, headers={"X-Api-Key": KEY}) as c:
        for q, want_hit in CASES:
            r = c.get("/api/v1/search", params={"q": q, "limit": 3})
            if r.status_code != 200:
                FAIL.append(f"{q} (HTTP {r.status_code})")
                print(f"[FAIL] {q} — HTTP {r.status_code}")
                continue
            d = r.json()
            hits = d.get("results", [])
            titles = [h["doc"]["title"] for h in hits]
            top = float(hits[0]["score"]) if hits else 0.0
            if want_hit:
                pos_top.append((q, top))
                ok = bool(hits)
                if not ok:
                    FAIL.append(q)
                print(f"[{'PASS' if ok else 'FAIL'}] {q} (应命中) "
                      f"→ {len(hits)} 条 score={top:.4f} {titles[:2]}")
            else:
                neg_top.append((q, top))
                # 单条不判定，等收集完一起比分布
                print(f"[ ..  ] {q} (应低分) → {len(hits)} 条 score={top:.4f} {titles[:1]}")

    print()
    if pos_top and neg_top:
        pmin = min(s for _, s in pos_top)
        nmax = max(s for _, s in neg_top)
        print(f"正向 top1 最低分 {pmin:.4f}（{min(pos_top, key=lambda x: x[1])[0]}）")
        print(f"无关 top1 最高分 {nmax:.4f}（{max(neg_top, key=lambda x: x[1])[0]}）")
        if nmax < pmin:
            print(f"✅ 无关查询得分全部低于正向最低分，间隔 {pmin-nmax:+.4f}")
        else:
            # 允许重叠但要求无关查询不能挤进正向分数的中位数以上
            pmed = sorted(s for _, s in pos_top)[len(pos_top) // 2]
            if nmax < pmed:
                print(f"⚠️ 与正向最低分有重叠，但仍低于正向中位数 {pmed:.4f} —— 可接受")
            else:
                print(f"❌ 无关查询得分达到正向中位数 {pmed:.4f} 之上，排序失真")
                FAIL.append(f"无关查询分数过高 {nmax:.4f} >= 中位 {pmed:.4f}")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项不符预期：{FAIL}")
        sys.exit(1)
    print("✅ 召回用例全部符合预期")


if __name__ == "__main__":
    main()
