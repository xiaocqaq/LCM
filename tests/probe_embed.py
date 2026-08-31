"""探测网关 embedding 能力：列模型 + 逐个试 /embeddings。

跑法：cd /opt/memorys && .venv/bin/python tests/probe_embed.py
"""
import json
import pathlib
import re

import httpx

CFG = pathlib.Path("/root/.hermes/profiles/xiao/config.yaml").read_text()
m = re.search(r"api_key:\s*(sk-[A-Za-z0-9_\-]+)", CFG)
KEY = m.group(1)
BASE = "https://xiao.xlingo.fun/v1"

CANDIDATES = [
    "text-embedding-3-small",
    "text-embedding-3-large",
    "text-embedding-ada-002",
    "text-embedding-v4",
    "bge-m3",
    "Qwen3-Embedding-8B",
    "doubao-embedding-large",
]


def main():
    h = {"Authorization": f"Bearer {KEY}"}
    with httpx.Client(timeout=30) as c:
        try:
            r = c.get(f"{BASE}/models", headers=h)
            ids = [d["id"] for d in r.json().get("data", [])]
            emb = [i for i in ids if "embed" in i.lower() or "bge" in i.lower()]
            print(f"/models -> {r.status_code}, 共 {len(ids)} 个模型")
            print("疑似 embedding 模型:", emb if emb else "（模型列表里没有明显的 embedding）")
            for cand in emb:
                if cand not in CANDIDATES:
                    CANDIDATES.insert(0, cand)
        except Exception as e:
            print("/models 失败:", repr(e)[:120])

        print("\n=== 逐个试 /embeddings ===")
        for model in CANDIDATES:
            try:
                r = c.post(f"{BASE}/embeddings", headers=h,
                           json={"model": model, "input": ["测试中文向量"]})
                if r.status_code == 200:
                    vec = r.json()["data"][0]["embedding"]
                    print(f"✅ {model}: dim={len(vec)}")
                else:
                    msg = r.text[:100].replace("\n", " ")
                    print(f"❌ {model}: {r.status_code} {msg}")
            except Exception as e:
                print(f"❌ {model}: {repr(e)[:100]}")


if __name__ == "__main__":
    main()
