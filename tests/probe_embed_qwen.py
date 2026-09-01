#!/usr/bin/env python3
"""实测 qwen3.7-text-embedding-flash 是否够用来做 memorys 的向量检索。

不看模型名下结论：中转站的模型名跟实际后端未必对得上。
逐项验证：可用性 / 维度 / 批量上限 / 归一化 / 中文语义质量 / 延迟。
"""
import json, time, urllib.request, math, sys

BASE = "https://xiao.xlingo.fun/v1"
KEY = open("/tmp/oct.key").read().strip()
MODEL = "qwen3.7-text-embedding-flash"


def embed(texts, model=MODEL, timeout=90):
    body = json.dumps({"model": model, "input": texts}).encode()
    req = urllib.request.Request(
        BASE + "/embeddings", data=body,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d, time.time() - t0


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb)


print("=" * 68)
print("1. 基本可用性 + 维度")
print("=" * 68)
try:
    d, dt = embed(["测试一段中文文本"])
    v = d["data"][0]["embedding"]
    DIM = len(v)
    norm = math.sqrt(sum(x * x for x in v))
    print(f"  ✅ 可用  维度={DIM}  L2范数={norm:.4f}  延迟={dt*1000:.0f}ms")
    print(f"     已归一化={'是' if abs(norm-1) < 0.01 else '否（需自己归一化或用 cosine 距离）'}")
    print(f"     usage={d.get('usage')}  实际模型={d.get('model')}")
except Exception as e:
    print(f"  ❌ 不可用: {type(e).__name__} {e}")
    sys.exit(1)

print()
print("=" * 68)
print("2. 批量上限（memorys 全量 reindex 要批量提交）")
print("=" * 68)
for n in (2, 8, 32, 64, 128):
    try:
        d, dt = embed([f"第{i}段测试文本，内容各不相同，用于测试批量嵌入能力" for i in range(n)])
        got = len(d["data"])
        ok = got == n
        print(f"  batch={n:<4} {'✅' if ok else '⚠️'} 返回 {got} 条  {dt*1000:.0f}ms  ({dt/n*1000:.0f}ms/条)")
        if not ok:
            break
    except Exception as e:
        print(f"  batch={n:<4} ❌ {type(e).__name__} {str(e)[:80]}")
        break

print()
print("=" * 68)
print("3. 中文语义质量（关键：这决定值不值得上）")
print("=" * 68)
print("  同义对应该高分，无关对应该低分，两者要拉开差距\n")

pos_pairs = [
    ("数据存在哪里", "Markdown 文件是 source of truth，PG 只做索引"),
    ("怎么换语音合成服务", "TTS 选型决策：自建 Kokoro 与云端 MiMo 的取舍"),
    ("前端权限报错弹窗", "陷阱：调 requireSuperAdmin 端点会触发全局 403 弹窗风暴"),
    ("大数据查询慢怎么优化", "frcws-service cmos_base_account_detail 大数据查询优化"),
    ("新人怎么快速了解这个项目", "xspeak 项目架构总结"),
    ("什么时候该给用户提示", "引导时机：要在用户开口前"),
]
neg_pairs = [
    ("孜然羊肉怎么做", "Markdown 文件是 source of truth，PG 只做索引"),
    ("演唱会门票怎么抢", "TTS 选型决策：自建 Kokoro 与云端 MiMo 的取舍"),
    ("柴犬为什么拆家", "frcws-service cmos_base_account_detail 大数据查询优化"),
    ("郑州明天天气", "xspeak 项目架构总结"),
]

allt = [t for p in pos_pairs + neg_pairs for t in p]
d, dt = embed(allt)
vecs = [x["embedding"] for x in d["data"]]
print(f"  一次嵌入 {len(allt)} 段，耗时 {dt*1000:.0f}ms\n")

pos_scores, neg_scores = [], []
print("  【同义对】")
for i, (q, doc) in enumerate(pos_pairs):
    s = cos(vecs[i * 2], vecs[i * 2 + 1])
    pos_scores.append(s)
    print(f"    {s:.4f}  {q}  ←→  {doc[:32]}")
off = len(pos_pairs) * 2
print("  【无关对】")
for i, (q, doc) in enumerate(neg_pairs):
    s = cos(vecs[off + i * 2], vecs[off + i * 2 + 1])
    neg_scores.append(s)
    print(f"    {s:.4f}  {q}  ←→  {doc[:32]}")

pmin, pavg = min(pos_scores), sum(pos_scores) / len(pos_scores)
nmax, navg = max(neg_scores), sum(neg_scores) / len(neg_scores)
gap = pmin - nmax
print()
print(f"  同义：最低 {pmin:.4f}  平均 {pavg:.4f}")
print(f"  无关：最高 {nmax:.4f}  平均 {navg:.4f}")
print(f"  最坏情况间隔 = {gap:+.4f}   平均间隔 = {pavg-navg:+.4f}")
if gap > 0.08:
    verdict = "✅ 可分性good，能设阈值"
elif gap > 0:
    verdict = "⚠️ 可分但间隔窄，阈值难定，靠 RRF 融合还行"
else:
    verdict = "❌ 无关对得分超过了同义对，语义质量不合格"
print(f"  → {verdict}")

print()
print("=" * 68)
print("4. 稳定性（同一文本两次调用应完全一致，否则不能缓存）")
print("=" * 68)
d1, _ = embed(["稳定性测试文本"])
d2, _ = embed(["稳定性测试文本"])
same = cos(d1["data"][0]["embedding"], d2["data"][0]["embedding"])
print(f"  两次调用余弦相似度 = {same:.8f}  {'✅ 确定性，可缓存' if same > 0.9999 else '⚠️ 不确定，缓存会漂移'}")

print()
print("=" * 68)
print("5. 长文本截断行为（memorys 的 chunk 最长约 2000 字符）")
print("=" * 68)
for n in (500, 2000, 8000):
    try:
        d, dt = embed(["中" * n])
        print(f"  {n} 字符  ✅ {dt*1000:.0f}ms  usage={d.get('usage',{}).get('total_tokens')}")
    except Exception as e:
        print(f"  {n} 字符  ❌ {type(e).__name__} {str(e)[:70]}")

print()
print("=" * 68)
print(f"结论输入：维度 {DIM}，可分性间隔 {gap:+.4f}")
print("=" * 68)
