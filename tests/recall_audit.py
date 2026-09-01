"""检索质量实测：拿真实库跑一批"人会怎么问"的查询，量化关键词检索的命中率。

目的是给"要不要上向量检索"提供依据，而不是凭感觉。
分三类查询：
  A 概念词类  —— 正文写具体形态，提问用概念词（索引侧扩展应该能覆盖）
  B 同义改写类 —— 用完全不同的词表达同一件事（关键词的软肋）
  C 跨篇推理类 —— 答案需要综合多篇（任何检索都难）

跑法：cd /opt/memorys && .venv/bin/python tests/recall_audit.py
"""
import json
import pathlib

import httpx

KEY = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
BASE = "https://repo.xlingo.fun"
H = {"X-Api-Key": KEY}

# (查询, 期望命中的文档标题关键片段, 类别)
CASES = [
    # A 类：概念词 → 具体形态
    ("记忆文件存在哪个目录", "memorys 上线记录", "A"),
    ("服务监听哪个端口", "memorys 上线记录", "A"),
    ("怎么鉴权", "memorys 上线记录", "A"),
    ("xspeak 部署在哪", "xspeak 项目架构", "A"),
    ("前端发布流程", "hs.xlingo.fun", "A"),

    # B 类：同义改写（不共享字面词）
    ("语音合成用哪家", "TTS 选型决策", "B"),
    ("朗读功能选型", "TTS 选型决策", "B"),
    ("提示应该什么时候给用户看", "引导时机", "B"),
    ("套接字文件丢了怎么办", "IPC socket", "B"),
    ("管理员接口报权限错误", "requireSuperAdmin", "B"),
    ("查询太慢怎么优化", "cmos_base_account_detail", "B"),
    ("换行符问题", "Windows 本地开发", "B"),
    ("怎么把改动同步到线上", "hs.xlingo.fun", "B"),
    ("大表统计耗时", "cmos_base_account_detail", "B"),
    ("弹窗一直冒出来", "requireSuperAdmin", "B"),

    # C 类：跨篇/推理
    ("这个项目有哪些坑", None, "C"),
    ("我做过哪些技术选型", None, "C"),
]


def main():
    rows = []
    with httpx.Client(base_url=BASE, timeout=60, headers=H) as c:
        for q, expect, cat in CASES:
            r = c.get("/api/v1/search", params={"q": q, "limit": 5})
            hits = r.json().get("results", []) if r.status_code == 200 else []
            titles = []
            for h in hits:
                t = h["doc"]["title"]
                if t not in titles:
                    titles.append(t)
            hit = None
            if expect:
                hit = any(expect in t for t in titles)
                rank = next((i + 1 for i, t in enumerate(titles) if expect in t), None)
            else:
                rank = None
            rows.append({"q": q, "cat": cat, "expect": expect, "hit": hit,
                         "rank": rank, "n": len(hits), "titles": titles[:3]})

    for cat in "ABC":
        sub = [r for r in rows if r["cat"] == cat]
        scored = [r for r in sub if r["expect"]]
        name = {"A": "概念词→具体形态", "B": "同义改写", "C": "跨篇/推理"}[cat]
        if scored:
            ok = sum(1 for r in scored if r["hit"])
            top1 = sum(1 for r in scored if r["rank"] == 1)
            print(f"\n=== {cat} 类 {name}：命中 {ok}/{len(scored)}，"
                  f"排第一 {top1}/{len(scored)} ===")
        else:
            print(f"\n=== {cat} 类 {name}（无固定答案，看返回内容是否相关）===")
        for r in sub:
            mark = "✅" if r["hit"] else ("❌" if r["expect"] else "—")
            pos = f" rank={r['rank']}" if r["rank"] else ""
            print(f"  {mark} {r['q']}{pos}")
            print(f"       返回 {r['n']} 条 → {r['titles']}")

    scored = [r for r in rows if r["expect"]]
    ok = sum(1 for r in scored if r["hit"])
    print(f"\n总计（有明确答案的 {len(scored)} 条）：命中 {ok}，miss {len(scored)-ok}，"
          f"命中率 {ok/len(scored)*100:.0f}%")
    pathlib.Path("/tmp/recall_audit.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
