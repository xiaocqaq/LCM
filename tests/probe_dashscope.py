#!/usr/bin/env python3
"""实测阿里云百炼 embedding 是否够用来做 memorys 的向量检索。

不看模型名下结论。逐项验证：真实可用模型名 / 维度 / 批量上限 / 中文语义质量 / 延迟。
key 从 /root/.hermes/profiles/xiao/.env 的 DASHSCOPE_API_KEY 读，不落盘不打印。
"""
import json, time, urllib.request, urllib.error, math, os, re, sys

BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

KEY = ""
for ln in open("/root/.hermes/profiles/xiao/.env"):
    m = re.match(r"\s*DASHSCOPE_API_KEY\s*=\s*(\S+)", ln)
    if m:
        KEY = m.group(1).strip().strip("\"'")
        break
if not KEY:
    print("❌ 没读到 DASHSCOPE_API_KEY")
    sys.exit(1)
print(f"key 前缀 {KEY[:6]}…  长度 {len(KEY)}\n")


def embed(texts, model, dim=None, timeout=90):
    payload = {"model": model, "input": texts, "encoding_format": "float"}
    if dim:
        payload["dimensions"] = dim
    req = urllib.request.Request(
        BASE + "/embeddings", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), time.time() - t0


def err(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            b = json.loads(e.read().decode())
            return f"HTTP {e.code} {b.get('error',{}).get('message', b)[:110]}"
        except Exception:
            return f"HTTP {e.code}"
    return f"{type(e).__name__} {str(e)[:90]}"


def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b)))


print("=" * 70)
print("1. 哪些模型名真的能用（用户在 UI 里写的是 qwen3.7-text-embedding）")
print("=" * 70)
CANDIDATES = [
    "qwen3.7-text-embedding-flash",
    "qwen3.7-text-embedding",
    "text-embedding-v4",
    "text-embedding-v3",
]
usable = []
for m in CANDIDATES:
    try:
        d, dt = embed(["测试"], m)
        v = d["data"][0]["embedding"]
        usable.append((m, len(v)))
        print(f"  ✅ {m:<34} 维度 {len(v):<5} {dt*1000:.0f}ms  返回模型={d.get('model')}")
    except Exception as e:
        print(f"  ❌ {m:<34} {err(e)}")

if not usable:
    print("\n全部不可用，停止")
    sys.exit(1)

MODEL, DIM = usable[0]
print(f"\n→ 采用 {MODEL}（维度 {DIM}）")

print()
print("=" * 70)
print("2. 维度可否自定义（memorys 的 hnsw 索引现在建在 1536 维上）")
print("=" * 70)
for want in (1536, 1024, 768):
    try:
        d, dt = embed(["测试"], MODEL, dim=want)
        got = len(d["data"][0]["embedding"])
        print(f"  dimensions={want:<5} → 实际 {got:<5} {'✅ 支持' if got == want else '⚠️ 被忽略'}")
    except Exception as e:
        print(f"  dimensions={want:<5} ❌ {err(e)}")

print()
print("=" * 70)
print("3. 归一化与确定性（决定能否直接用内积、能否缓存）")
print("=" * 70)
d1, _ = embed(["稳定性测试文本"], MODEL)
d2, _ = embed(["稳定性测试文本"], MODEL)
v1 = d1["data"][0]["embedding"]
norm = math.sqrt(sum(x * x for x in v1))
same = cos(v1, d2["data"][0]["embedding"])
print(f"  L2 范数 = {norm:.6f}  {'✅ 已归一化' if abs(norm-1) < 0.01 else '⚠️ 未归一化，必须用 cosine 不能用内积'}")
print(f"  两次调用相似度 = {same:.8f}  {'✅ 确定性，可缓存' if same > 0.9999 else '⚠️ 有随机性'}")

print()
print("=" * 70)
print("4. 批量上限（reindex 要批量提交，逐条会很慢）")
print("=" * 70)
BATCH_OK = 1
for n in (10, 25, 50, 100):
    try:
        d, dt = embed([f"第{i}段测试文本，内容各不相同用于测试批量能力" for i in range(n)], MODEL)
        got = len(d["data"])
        if got == n:
            BATCH_OK = n
            print(f"  batch={n:<5} ✅ {dt*1000:.0f}ms  ({dt/n*1000:.0f}ms/条)")
        else:
            print(f"  batch={n:<5} ⚠️ 只返回 {got} 条")
            break
    except Exception as e:
        print(f"  batch={n:<5} ❌ {err(e)}")
        break
print(f"  → 可用批量 {BATCH_OK}")

print()
print("=" * 70)
print("5. 中文语义质量 —— 这一项决定值不值得上")
print("=" * 70)
print("  用真实库里的文档标题做正向对，无关话题做反向对\n")

pos = [
    ("数据存在哪里", "Markdown 文件是 source of truth，PG 只做索引，可从磁盘全量重建"),
    ("怎么换语音合成服务", "TTS 选型决策：自建 Kokoro 与云端 MiMo 的取舍"),
    ("前端权限报错弹窗", "陷阱：前端调 requireSuperAdmin 端点会触发全局 403 弹窗风暴"),
    ("大数据查询慢怎么优化", "frcws-service cmos_base_account_detail 大数据查询优化上下文"),
    ("新人怎么快速了解这个项目", "xspeak 项目架构总结"),
    ("什么时候该给用户提示", "引导时机：要在用户开口前"),
    ("换电脑后环境不一样", "Windows 本地开发：行尾、jsdom realm 与测试构建命令"),
    ("进程socket没了怎么恢复", "Hermes Agent Bridge IPC socket 被清理后的恢复与预防"),
]
neg = [
    ("孜然羊肉怎么做", "Markdown 文件是 source of truth，PG 只做索引，可从磁盘全量重建"),
    ("演唱会门票怎么抢", "TTS 选型决策：自建 Kokoro 与云端 MiMo 的取舍"),
    ("柴犬为什么拆家", "frcws-service cmos_base_account_detail 大数据查询优化上下文"),
    ("郑州明天天气怎么样", "xspeak 项目架构总结"),
    ("孕妇能吃螃蟹吗", "引导时机：要在用户开口前"),
]

allt = [t for p in pos + neg for t in p]
# 批量上限 25，必须分批（实测坑：26 条就 400）
V, total_dt = [], 0.0
for i in range(0, len(allt), 25):
    d, dt = embed(allt[i:i + 25], MODEL)
    V.extend(x["embedding"] for x in d["data"])
    total_dt += dt
print(f"  嵌入 {len(allt)} 段（分 {math.ceil(len(allt)/25)} 批），{total_dt*1000:.0f}ms\n")

ps, ns = [], []
print("  【应该命中】")
for i, (q, doc) in enumerate(pos):
    s = cos(V[i * 2], V[i * 2 + 1]); ps.append(s)
    print(f"    {s:.4f}  {q}")
off = len(pos) * 2
print("  【不该命中】")
for i, (q, doc) in enumerate(neg):
    s = cos(V[off + i * 2], V[off + i * 2 + 1]); ns.append(s)
    print(f"    {s:.4f}  {q}")

pmin, pavg = min(ps), sum(ps) / len(ps)
nmax, navg = max(ns), sum(ns) / len(ns)
gap = pmin - nmax
print(f"\n  正向 最低 {pmin:.4f} 平均 {pavg:.4f}")
print(f"  反向 最高 {nmax:.4f} 平均 {navg:.4f}")
print(f"  最坏间隔 {gap:+.4f}   平均间隔 {pavg-navg:+.4f}")
if gap > 0.10:
    v = "✅ 可分性好，能设绝对阈值过滤"
elif gap > 0.02:
    v = "✅ 可分，间隔偏窄；靠 RRF 融合排名而非绝对阈值即可"
elif gap > 0:
    v = "⚠️ 勉强可分，只能靠 RRF，不要设阈值"
else:
    v = "❌ 反向超过正向，语义质量不合格"
print(f"  → {v}")

print()
print("=" * 70)
print("6. 长文本（memorys 的 chunk 最长约 2000 字符）")
print("=" * 70)
for n in (500, 2000, 4000):
    try:
        d, dt = embed(["中" * n], MODEL)
        print(f"  {n} 字符 ✅ {dt*1000:.0f}ms  tokens={d.get('usage',{}).get('total_tokens')}")
    except Exception as e:
        print(f"  {n} 字符 ❌ {err(e)}")

print()
print("=" * 70)
print(f"结论输入：模型={MODEL} 维度={DIM} 批量={BATCH_OK} 最坏间隔={gap:+.4f}")
print("=" * 70)
