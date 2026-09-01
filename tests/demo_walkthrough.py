"""演示：AI 跨会话记忆的完整用法（存 → 检索 → 换会话恢复上下文）。

模拟真实场景：会话 A 里 agent 沉淀了 xspeak 项目的关键信息，
会话 B（换了模型/换了 agent）靠 bootstrap 一次性恢复。

跑法：cd /opt/memorys && .venv/bin/python tests/demo_walkthrough.py
"""
import json
import pathlib

import httpx

KEY = pathlib.Path("/root/.memorys-hermes-key").read_text().strip()
BASE = "https://repo.xlingo.fun"
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}

# 会话 A：agent 边干活边沉淀。三种典型记忆类型。
SEEDS = [
    {
        "title": "xspeak 项目架构",
        "type": "project_summary",
        "project": "xspeak",
        "tags": ["nextjs", "deploy"],
        "importance": 5,
        "content": """## 是什么
口语练习应用，生产站 speak.xlingo.fun，代码在 /opt/xspeak，端口 3022。

## 结构
Next.js + PG。生产是独立源码副本+独立构建，NEXT_PUBLIC_BASE_PATH 为空；
测试站 /xlearn 走另一份构建。

## 发版
照 HANDOFF.md「生产部署」节。同步源码时 tar 必须排除 .env.local/.cache/data。
""",
    },
    {
        "title": "TTS 选型决策",
        "type": "decision",
        "project": "xspeak",
        "tags": ["tts"],
        "importance": 5,
        "content": """## 结论
分梯队：只有凌晨 4 点的 cron 用自建 Kokoro，其余全走云端 MiMo。

## 依据
自建 Kokoro 单次 9-10s 且偶发 502；云端 MiMo 3.3s。
实时交互扛不住 10s，离线批量任务不在乎。

## 坑
users.voice 存浏览器包名会被当成"用户显式选了本地 TTS"，导致在线 TTS 永不生效。
已清库 + 上线时清残留逻辑。
""",
    },
    {
        "title": "引导时机：要在用户开口前",
        "type": "preference",
        "project": "xspeak",
        "tags": ["ux"],
        "importance": 4,
        "content": """通话页的提示必须在学生开口**之前**就出现。
事后再提示的 v1 方案被否掉了——引导要在用户需要之前就位，事后提示等于没提示。
""",
    },
]


def sect(t):
    print(f"\n{'=' * 60}\n{t}\n{'=' * 60}")


def main():
    with httpx.Client(base_url=BASE, timeout=60, headers=H) as c:
        sect("① 会话 A：agent 沉淀记忆（memory_write）")
        ids = []
        for s in SEEDS:
            r = c.post("/api/v1/documents", json=s)
            if r.status_code == 409:  # 已存在，跑第二遍时走这
                print(f"  已存在，跳过：{s['title']}")
                continue
            r.raise_for_status()
            d = r.json()
            ids.append(d["id"])
            print(f"  ✓ [{d['id']}] {d['title']}  ({s['type']}, importance={s['importance']})")

        sect("② 用自然语言检索（memory_search）")
        for q in ["TTS 用哪个", "生产站怎么发版", "提示什么时候出现"]:
            r = c.get("/api/v1/search", params={"q": q, "limit": 2})
            hits = r.json().get("results", [])
            print(f"\n  问：{q}")
            for h in hits:
                head = h.get("heading") or "(正文)"
                body = " ".join(h["content"].split())[:70]
                print(f"    → 《{h['doc']['title']}》/{head}  score={h['score']:.4f}")
                print(f"      {body}…")

        sect("③ 会话 B：换模型/换 agent，一次性恢复上下文（memory_bootstrap）")
        r = c.get("/api/v1/bootstrap", params={"project": "xspeak", "token_budget": 1500})
        b = r.json()
        print(f"  范围：{b['scope_label']}   选中 {b['document_count']}/{b['total_documents']} 篇"
              f"   约 {b['estimated_tokens']}/{b['token_budget']} tokens")
        print("\n  ---- digest：这段直接塞进新会话开场 ----")
        for line in b["digest"].splitlines()[:24]:
            print(f"  {line}")
        n = len(b["digest"].splitlines())
        if n > 24:
            print(f"  …（共 {n} 行）")

        sect("④ 版本历史（memory_history）：每次写入自动 git commit")
        if ids:
            for h in c.get(f"/api/v1/documents/{ids[0]}/history",
                           params={"limit": 3}).json():
                print(f"  {h['hash']}  {h['date']}  {h['message']}")

        sect("⑤ 当前库存量（memory_list_docs）")
        d = c.get("/api/v1/documents", params={"limit": 20}).json()
        for it in d["items"]:
            print(f"  [{it['id']:>3}] {it['title']:<28} {it['type']:<16} "
                  f"proj={it.get('project') or '-':<10} imp={it['importance']}")
        print(f"\n  共 {d['total']} 篇")


if __name__ == "__main__":
    main()
