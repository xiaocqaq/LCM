#!/usr/bin/env python3
"""上游 embedding 现在是什么状态？直接测，别猜。

之前 sync 卡住 16 分钟连一篇都没跑完，怀疑上游整体挂了 ——
挂了的话每批要走满 2 次重试 × 30s 超时，再拆单条又是每条 2×30s。
"""
import json
import re
import time
import urllib.error
import urllib.request

BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
KEY = next(re.match(r"\s*MEM_EMBED_API_KEY\s*=\s*(\S+)", l).group(1).strip("\"'")
           for l in open("/opt/memorys/.env")
           if re.match(r"\s*MEM_EMBED_API_KEY\s*=\s*\S", l))
MODEL = "qwen3.7-text-embedding-flash"

print("连续 8 次单条调用，看成功率与延迟：\n")
ok = 0
lat = []
for i in range(8):
    body = json.dumps({"model": MODEL, "input": [f"第{i}次探测文本，内容各不相同"],
                       "encoding_format": "float", "dimensions": 1024}).encode()
    req = urllib.request.Request(BASE, data=body, headers={
        "Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        dt = time.time() - t0
        lat.append(dt)
        ok += 1
        print(f"  {i+1}. ✅ {dt*1000:>6.0f}ms  维度 {len(d['data'][0]['embedding'])}")
    except urllib.error.HTTPError as e:
        print(f"  {i+1}. ❌ HTTP {e.code} {e.read()[:120].decode(errors='replace')}")
    except Exception as e:
        print(f"  {i+1}. ❌ {type(e).__name__} {str(e)[:70]}  ({time.time()-t0:.1f}s)")

print()
print(f"成功 {ok}/8", end="")
if lat:
    print(f"，延迟 中位 {sorted(lat)[len(lat)//2]*1000:.0f}ms 最大 {max(lat)*1000:.0f}ms")
else:
    print("  ← 上游整体不可用")
print()
if ok == 0:
    print("结论：上游挂了。写入侧每批会走满重试再拆单条，"
          "一篇 30 chunk 的文档最坏要等 2×30 + 30×2×30 = 1860s。")
    print("      这就是 sync 超时的根因 —— 不是慢，是在等一个死掉的服务。")
elif ok < 8:
    print(f"结论：上游不稳定（{ok}/8）。重试逻辑会放大等待时间。")
else:
    print("结论：上游正常。sync 慢是别的原因。")
