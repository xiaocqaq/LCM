#!/usr/bin/env python3
"""stdio 传输的端到端测试：真的起一个子进程，按 JSON-RPC 跟它对话。

不 mock 任何东西 —— 用的就是 MCP 客户端会用的那套调用方式：
拉起 `python -m app.mcp_stdio`，往 stdin 写请求，从 stdout 读响应。

重点验的是 stdio 特有的失败模式：
  - stdout 被日志污染 → 客户端解析失败（最常见）
  - 库没初始化 → 第一次调工具就 no such table
  - server 模式无凭证时必须拒绝，不能猜用户
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
OK = FAIL = 0


def chk(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


# 这些变量必须从子进程环境里剔掉。
#
# 实测踩到：acceptance.sh 会 source 生产 .env（好让需要 PG 的套件能跑），
# 于是 MEM_DATA_DIR / MEM_MODE / MEM_DATABASE_URL 被继承进来，
# 本测试的 md 就写到了**生产数据目录**而不是自己的临时 home ——
# 表现是"md 落盘"和"git 有提交记录"两条失败，看着像 stdio 写入坏了，
# 实际是测试在往别人的目录里写。单跑时环境干净所以一直没暴露。
_STRIP = (
    "MEM_MODE", "MEM_DATA_DIR", "MEM_DATABASE_URL", "MEM_LOCAL_HOME",
    "MEM_JWT_SECRET", "MEM_API_KEY", "MEM_MCP_TOKEN", "MEM_ENV_FILE",
    "MEM_LOCAL_OPEN", "MEM_LOCAL_USER",
)


def _clean_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    env.update(extra)
    return env


class Client:
    """最小 stdio MCP 客户端。"""

    def __init__(self, env: dict):
        self.p = subprocess.Popen(
            [PY, "-m", "app.mcp_stdio"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_clean_env(env),
            text=True,
            bufsize=1,
        )
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: int = 30):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        # 逐行读，跳过通知（没有 id 的消息）
        for _ in range(50):
            line = self.p.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 这就是 stdout 被污染的症状，原样报出来
                return {"_parse_error": line[:200]}
            if msg.get("id") == self._id:
                return msg
        return None

    def notify(self, method: str, params: dict | None = None):
        req = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()

    def stderr(self) -> str:
        try:
            return self.p.stderr.read() or ""
        except Exception:
            return ""


def main() -> int:
    home = tempfile.mkdtemp(prefix="memstdio-")
    env = {"MEM_MODE": "local", "MEM_LOCAL_HOME": home}
    print(f"=== stdio 测试（全新库 {home}）===")

    c = Client(env)
    try:
        # 1. 握手
        r = c.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "stdio-test", "version": "1"},
        })
        chk("initialize 有响应", r is not None, "无响应")
        chk("stdout 没被日志污染", bool(r) and "_parse_error" not in r,
            (r or {}).get("_parse_error", ""))
        srv = ((r or {}).get("result") or {}).get("serverInfo") or {}
        chk("serverInfo.name == memorys", srv.get("name") == "memorys", str(srv))
        c.notify("notifications/initialized")

        # 2. 工具清单
        r = c.call("tools/list")
        tools = [t["name"] for t in (((r or {}).get("result") or {}).get("tools") or [])]
        chk("10 个工具", len(tools) == 10, f"实得 {len(tools)}: {tools}")
        for t in ("memory_search", "memory_write", "memory_get", "memory_bootstrap"):
            chk(f"有 {t}", t in tools)

        # 3. 全新库直接调工具（验初始化：没建表这里就炸）
        r = c.call("tools/call", {"name": "memory_list_libraries", "arguments": {}})
        res = (r or {}).get("result") or {}
        txt = "".join(b.get("text", "") for b in (res.get("content") or []))
        chk("全新库能调工具（表已建好）", "no such table" not in txt.lower(), txt[:160])
        chk("list_libraries 不报错", not res.get("isError"), txt[:160])

        # 4. 写 → 检索 → 读回
        r = c.call("tools/call", {"name": "memory_write", "arguments": {
            "title": "stdio 冒烟",
            "content": "# stdio\n\n通过 stdio 传输写入的一条记忆，用来验证端到端链路。\n",
            "type": "howto", "project": "stdio-test", "importance": 4,
        }})
        res = (r or {}).get("result") or {}
        wtxt = "".join(b.get("text", "") for b in (res.get("content") or []))
        chk("memory_write 成功", not res.get("isError"), wtxt[:200])
        doc_id = None
        try:
            doc_id = json.loads(wtxt).get("id")
        except Exception:
            pass
        chk("write 返回 id", bool(doc_id), wtxt[:160])

        r = c.call("tools/call", {"name": "memory_search",
                                  "arguments": {"query": "stdio 传输", "limit": 3}})
        stxt = "".join(b.get("text", "")
                       for b in (((r or {}).get("result") or {}).get("content") or []))
        hit = False
        try:
            hit = json.loads(stxt).get("count", 0) > 0
        except Exception:
            pass
        chk("检索命中刚写的内容", hit, stxt[:200])

        if doc_id:
            r = c.call("tools/call", {"name": "memory_get",
                                      "arguments": {"doc_id": int(doc_id)}})
            gtxt = "".join(b.get("text", "")
                           for b in (((r or {}).get("result") or {}).get("content") or []))
            chk("memory_get 拿到全文", "stdio" in gtxt, gtxt[:160])

        # 5. md 真落盘 + git 有提交
        mds = list(Path(home).rglob("*.md"))
        chk("md 落盘", len(mds) >= 1, f"找到 {len(mds)} 个")
        gitdirs = list(Path(home).rglob(".git"))
        chk("git repo 已建", len(gitdirs) >= 1, f"找到 {len(gitdirs)} 个")
        if gitdirs:
            log = subprocess.run(
                ["git", "-C", str(gitdirs[0].parent), "log", "--oneline"],
                capture_output=True, text=True).stdout.strip()
            chk("git 有提交记录", bool(log), "log 为空（try_commit_all 静默失败）")

        err = ""
    finally:
        c.close()
        err = c.stderr()

    chk("启动日志走的是 stderr", "[memorys] stdio 就绪" in err, err[-200:])

    # 6. server 模式必须拒绝，且理由要说得清
    #
    # 这里有两层拒绝，顺序是有意义的：
    #   a) 连 DB 配置都没有 → config 层就中止（比"凭证无效"更早、更准）
    #   b) 配置齐了但没凭证 → 才轮到 stdio 自己的凭证检查
    # 两层都要验，不然改动其中一层时另一层的回归发现不了。
    print("=== server 模式（a）连配置都没有 ===")
    p = subprocess.run(
        [PY, "-m", "app.mcp_stdio"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        input="", env=_clean_env({"MEM_MODE": "server",
                                  "MEM_DATABASE_URL": "", "MEM_JWT_SECRET": "",
                                  "MEM_API_KEY": "", "MEM_MCP_TOKEN": ""}),
    )
    chk("(a) 退出码非 0", p.returncode != 0, f"rc={p.returncode}")
    chk("(a) 指出缺哪个配置", "MEM_DATABASE_URL" in p.stderr, p.stderr[-200:])
    chk("(a) 给了本地模式的出路", "MEM_MODE=local" in p.stderr, p.stderr[-200:])

    print("=== server 模式（b）配置齐但无凭证 ===")
    # 给一个能过 config 校验但连不上的 DSN：凭证检查在建连接之前，
    # 所以这里根本不会去连库。
    p = subprocess.run(
        [PY, "-m", "app.mcp_stdio"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        input="", env=_clean_env({"MEM_MODE": "server",
                                  "MEM_DATABASE_URL":
                                      "postgresql+asyncpg://u:p@127.0.0.1:1/none",
                                  "MEM_JWT_SECRET": "x" * 32,
                                  "MEM_API_KEY": "", "MEM_MCP_TOKEN": ""}),
    )
    chk("(b) 退出码非 0", p.returncode != 0, f"rc={p.returncode}")
    chk("(b) 提示里说了要 MEM_API_KEY", "MEM_API_KEY" in p.stderr, p.stderr[-300:])
    chk("(b) 没有猜用户（不该出现 stdio 就绪）",
        "stdio 就绪" not in p.stderr, p.stderr[-300:])

    shutil.rmtree(home, ignore_errors=True)
    print(f"\nOK={OK} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
