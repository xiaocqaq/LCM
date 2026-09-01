"""search.py 单测：锁住"索引侧扩展、查询侧不扩"这个不对称契约。

这套逻辑很容易在后续优化里被"顺手改成两侧一致"，那样会导致查询膨胀、
把无关文档拉进结果。所以这里既断言扩展生效，也断言查询侧没被污染。

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/search_unit_test.py
"""
import sys

from app.search import _path_terms, expand_tokens, to_tsquery, tokenize

FAIL = []


def check(name, cond, detail: object = ""):
    if cond:
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


DOC = """# 存储
记忆 md 落在 /var/lib/memorys/data/users/u20/main/ 下，PG 只做索引。
服务 systemd 管理，监听 127.0.0.1:8649，nginx 反代 repo.xlingo.fun。
鉴权用 X-Api-Key，git 每次写入自动 commit。
agent 走 mcp 端点，共 10 个工具。
"""


def main():
    idx = expand_tokens(DOC)
    s = set(idx)

    # --- 索引侧：概念词被注入 ---
    check("路径出现 → 注入 目录/路径/文件夹/位置",
          {"目录", "路径", "文件夹", "位置"}.issubset(s), sorted(s))
    check("data → 数据", "数据" in s)
    check("users → 用户", "用户" in s)
    check("systemd → 服务/开机自启", {"服务", "开机自启"}.issubset(s))
    check("nginx → 反代/网关", {"反代", "网关"}.issubset(s))
    check("git → 版本/提交", {"版本", "提交"}.issubset(s))
    check("md → markdown/文档", {"markdown", "文档"}.issubset(s))
    check("同义链：目录 → 存放", "存放" in s)
    check("原文实词保留（memorys/8649）",
          {"memorys", "8649"}.issubset(s))

    # --- 路径噪声段被丢掉 ---
    check("路径噪声段 var/lib 不进索引",
          not {"var", "lib"}.intersection(set(_path_terms(DOC))),
          _path_terms(DOC))

    # --- 去重 ---
    check("索引词无重复", len(idx) == len(set(idx)),
          f"{len(idx)} vs {len(set(idx))}")

    # --- 查询侧：绝不扩展 ---
    q = tokenize("记忆文件存在哪个目录")
    check("查询侧不注入同义词（无 路径/文件夹）",
          not {"路径", "文件夹", "位置"}.intersection(set(q)), q)
    check("查询侧不做英文映射",
          "数据" not in tokenize("data 在哪"), tokenize("data 在哪"))
    check("to_tsquery 用 | 连接", " | " in to_tsquery("记忆 目录"))
    check("空输入安全", to_tsquery("") == "" and expand_tokens("") == [])

    # --- 停用词 ---
    check("停用词被剔除（怎么/如何/什么）",
          not {"怎么", "如何", "什么"}.intersection(set(tokenize("怎么如何什么部署"))))

    # --- limit 生效 ---
    check("limit 截断生效", len(tokenize("部署 " * 300, limit=10)) <= 10)

    # --- 回归：概念查询与索引有交集，无关查询无交集 ---
    for query in ["记忆文件存在哪个目录", "数据存在哪里", "md 文件放哪",
                  "MCP 怎么接入", "服务监听哪个端口", "怎么鉴权"]:
        ov = set(tokenize(query)).intersection(s)
        check(f"概念查询命中：{query}", bool(ov), f"重叠={ov}")
    # 这里对固定的 DOC 常量断言，所以「养猫要注意什么」是安全的
    # （DOC 里没有「注意」）。打真实库的 recall_public.py 就不能这么写——
    # 库里随时会进一篇带「注意事项」的技术文档，导致真命中被误判为失败。
    for query in ["养猫要注意什么", "今天天气怎么样", "股票行情"]:
        ov = set(tokenize(query)).intersection(s)
        check(f"无关查询不命中：{query}", not ov, f"重叠={ov}")

    print(f"\nFAIL={len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("✅ search 单测全部通过")


if __name__ == "__main__":
    main()
