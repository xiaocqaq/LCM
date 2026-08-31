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
    ("养猫要注意什么", False),
    ("今天天气怎么样", False),
    ("股票行情", False),
]

FAIL = []


def main():
    with httpx.Client(base_url=BASE, timeout=30, headers={"X-Api-Key": KEY}) as c:
        for q, want_hit in CASES:
            r = c.get("/api/v1/search", params={"q": q, "limit": 3})
            if r.status_code != 200:
                FAIL.append(f"{q} (HTTP {r.status_code})")
                print(f"[FAIL] {q} — HTTP {r.status_code}")
                continue
            d = r.json()
            hits = d.get("results", [])
            got = bool(hits)
            ok = got == want_hit
            if not ok:
                FAIL.append(q)
            titles = [h["doc"]["title"] for h in hits]
            exp = "应命中" if want_hit else "应不命中"
            print(f"[{'PASS' if ok else 'FAIL'}] {q} ({exp}) "
                  f"→ {len(hits)} 条 {titles[:2]}")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项不符预期：{FAIL}")
        sys.exit(1)
    print("✅ 召回用例全部符合预期")


if __name__ == "__main__":
    main()
