#!/usr/bin/env python3
"""补测长文本：上一轮 "中"*500 全超时，需要区分是重复字符的问题、限流、还是真的慢。"""
import json, time, urllib.request, urllib.error, re, math

BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL = "qwen3.7-text-embedding-flash"
KEY = next(re.match(r"\s*DASHSCOPE_API_KEY\s*=\s*(\S+)", l).group(1).strip("\"'")
           for l in open("/root/.hermes/profiles/xiao/.env")
           if re.match(r"\s*DASHSCOPE_API_KEY\s*=", l))

REAL = """memorys 是一个面向 AI 跨会话使用的知识库服务。Markdown 文件是 source of truth，
PostgreSQL 只做索引，任何时候都可以从磁盘全量重建。检索走三路 RRF 融合：jieba 分词后的
tsvector 关键词检索、pg_trgm 相似度兜底、以及可选的 embedding 向量检索。每个用户有独立的
git 仓库做版本化，每天凌晨一点由 systemd timer 推送到 GitHub 备份。分支写入用 git plumbing
而不是 checkout 来回切，避免翻动工作区。bootstrap 接口按 token 预算装箱返回压缩上下文包，
换模型或换 agent 后可以直接塞进新会话的开场。排序上加了长度归一化和 importance 加权，
因为纯 RRF 只看排名不看体量，长文会靠词汇覆盖面挤掉真正对题的短文。"""


def embed(texts, timeout=120, retries=2):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                BASE + "/embeddings",
                data=json.dumps({"model": MODEL, "input": texts,
                                 "encoding_format": "float"}).encode(),
                headers={"Authorization": "Bearer " + KEY,
                         "Content-Type": "application/json"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), time.time() - t0, attempt
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(2 + attempt * 3)


print("=" * 70)
print("A. 真实中文长文本（不是重复字符）")
print("=" * 70)
for mult, label in ((1, "~380 字符"), (3, "~1150 字符"), (6, "~2300 字符"), (12, "~4600 字符")):
    txt = (REAL * mult).replace("\n", "")
    try:
        d, dt, att = embed([txt])
        tok = d.get("usage", {}).get("total_tokens")
        print(f"  {len(txt):>5} 字符 ✅ {dt*1000:>6.0f}ms  tokens={tok}"
              f"{'  (重试 %d 次后成功)' % att if att else ''}")
    except Exception as e:
        print(f"  {len(txt):>5} 字符 ❌ {type(e).__name__} {str(e)[:60]}")

print()
print("=" * 70)
print("B. 重复字符是否特殊（上一轮 '中'*500 超时）")
print("=" * 70)
for n in (200, 500, 2000):
    try:
        d, dt, att = embed(["中" * n])
        print(f"  '中'×{n:<5} ✅ {dt*1000:>6.0f}ms  tokens={d.get('usage',{}).get('total_tokens')}"
              f"{'  (重试 %d 次)' % att if att else ''}")
    except Exception as e:
        print(f"  '中'×{n:<5} ❌ {type(e).__name__} {str(e)[:60]}")

print()
print("=" * 70)
print("C. 连续调用稳定性（模拟 reindex：64 个 chunk 分 3 批）")
print("=" * 70)
chunks = [f"这是第 {i} 个测试文档片段。{REAL[:200]}" for i in range(64)]
ok, fail, t_all = 0, 0, 0.0
for i in range(0, len(chunks), 25):
    b = chunks[i:i + 25]
    try:
        d, dt, att = embed(b)
        ok += len(d["data"]); t_all += dt
        print(f"  批 {i//25+1}（{len(b)} 条）✅ {dt*1000:>6.0f}ms"
              f"{'  (重试 %d 次)' % att if att else ''}")
    except Exception as e:
        fail += len(b)
        print(f"  批 {i//25+1}（{len(b)} 条）❌ {type(e).__name__} {str(e)[:50]}")
print(f"  → {ok} 成功 / {fail} 失败，总耗时 {t_all:.1f}s")
print(f"  全库 64 chunk 首次建向量预计 {t_all:.1f}s，可接受" if fail == 0
      else "  ⚠️ 有失败，写入侧必须容错降级")
