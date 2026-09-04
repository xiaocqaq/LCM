"""本地模式启动器：`python -m app.local`

设计目标只有一条：**在自己机器上零配置跑起来**。
不装 PostgreSQL、不配 secret、不建库、不建 API Key —— 那些负担属于服务器部署。
所以这个入口会自己把 MEM_MODE=local 设上，再把 uvicorn 拉起来。

    python -m app.local                    # 默认 127.0.0.1:8650，浏览器自动打开
    python -m app.local --port 9000
    python -m app.local --no-browser
    python -m app.local --data ~/mem-work  # 换数据目录（多套库互不干扰）
    python -m app.local --host 0.0.0.0     # 需要局域网访问，会强制要求鉴权

## 为什么默认端口是 8650 而不是 8649

8649 是服务器模式的端口。同一台机器上有人会两个都跑（本地库 + 连服务器的隧道），
撞端口的报错信息（`address already in use`）不会告诉你是这个原因。
差一位，肉眼也容易分辨日志是哪一边的。

## 安全边界

local 模式默认免鉴权（`MEM_LOCAL_OPEN=true`），意味着任何能连上这个端口的进程
都是那个单用户。所以绑定地址是有硬约束的：

- 绑 127.0.0.1 → 免鉴权，随便用
- 绑其他地址 → _guard_bind 会**拒绝启动**，除非显式关掉 local_open

这个检查刻意做成"拒绝启动"而不是"打个警告"：警告会被无视，
而一个免鉴权的知识库暴露在局域网上，泄露的是用户全部的项目记忆。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def _free_port(host: str, port: int, tries: int = 20) -> int:
    """端口被占就往后找。

    本地工具最烦人的失败模式是"端口被占 → 报错退出 → 用户不知道换哪个"。
    这里直接往后探，把实际用的端口打出来。
    """
    for i in range(tries):
        p = port + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise SystemExit(f"启动中止：{host}:{port}-{port + tries - 1} 全部被占用。用 --port 指定别的端口。")


def _guard_bind(host: str, local_open: bool) -> None:
    """免鉴权 + 非本机绑定 = 拒绝启动。见模块 docstring 的安全边界一节。"""
    loopback = host in ("127.0.0.1", "localhost", "::1", "")
    if loopback or not local_open:
        return
    sys.exit(
        f"启动中止：--host {host} 会让服务监听非本机地址，而本地模式默认是免鉴权的。\n"
        f"任何能连到这个端口的人都会被当成用户 "
        f"{os.environ.get('MEM_LOCAL_USER', 'local')}，能读写你全部的记忆。\n\n"
        f"想暴露到局域网，两步：\n"
        f"  1. 设 MEM_LOCAL_OPEN=false（打开鉴权）\n"
        f"  2. 先用 --host 127.0.0.1 起一次，在 Web UI 的「API Key」页建一个 Key\n"
        f"然后再用 --host {host} 启动，客户端带 X-API-Key 访问。"
    )


def _open_browser_later(url: str, delay: float = 1.2) -> None:
    """延迟开浏览器：立刻开会赶在 uvicorn 监听之前，用户看到连接被拒。"""
    def go():
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Timer(delay, go).start()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m app.local",
        description="memorys 本地模式：SQLite + 单用户免登录，零配置启动。",
    )
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，非本机地址需先关掉免鉴权）")
    ap.add_argument("--port", type=int, default=8650, help="监听端口（默认 8650，被占用会自动往后找）")
    ap.add_argument("--data", default="", help="数据目录（默认 ~/.memorys）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    ap.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    args = ap.parse_args(argv)

    # 环境变量必须在 import app.config 之前设好 —— config 是模块级求值的，
    # import 之后再改 os.environ 不会生效。这也是为什么这个文件顶部
    # 只 import 标准库：碰一下 app.* 就会连带 import config。
    os.environ["MEM_MODE"] = "local"
    if args.data:
        os.environ["MEM_LOCAL_HOME"] = str(Path(args.data).expanduser().resolve())
    os.environ.setdefault("MEM_APP_HOST", args.host)

    local_open = (os.environ.get("MEM_LOCAL_OPEN", "true").strip().lower()
                  not in ("false", "0", "no"))
    _guard_bind(args.host, local_open)

    port = _free_port(args.host, args.port)
    os.environ["MEM_APP_PORT"] = str(port)

    from .config import LOCAL_HOME, settings   # noqa: E402  见上面的顺序说明

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{port}/"
    print("─" * 62)
    print("  memorys 本地模式")
    print("─" * 62)
    print(f"  界面      {url}")
    print(f"  MCP       {url}mcp")
    print(f"  REST 文档 {url}api/docs")
    print(f"  数据      {LOCAL_HOME}")
    print(f"  数据库    {settings.database_url.split('///')[-1]}")
    print(f"  md 文件   {settings.data_dir}")
    print(f"  鉴权      {'关闭（仅本机可访问）' if local_open else '开启（需 API Key）'}")
    if not settings.embed_api_key:
        # 这一条必须说清楚：没配向量不是故障，检索照样能用。
        # 不说的话用户会以为哪里没装好。
        print("  向量检索  未配置 → 走关键词 + 模糊两路（够用；配 MEM_EMBED_* 可开第三路）")
    print("─" * 62)
    if port != args.port:
        print(f"  注意：{args.port} 被占用，改用 {port}")
    print("  Ctrl+C 停止")
    print()

    if not args.no_browser:
        _open_browser_later(url)

    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=port,
        reload=args.reload,
        # 本地单用户，一个 worker 足够；多 worker 反而让 SQLite 撞锁
        workers=1,
        log_level="warning",   # 本地不需要每条请求都打日志，噪音盖掉上面的提示
    )


if __name__ == "__main__":
    main()
