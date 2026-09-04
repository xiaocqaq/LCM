"""配置。环境变量前缀 MEM_，默认读项目根的 .env

⚠️ 这里**不放任何真实密钥**。密钥只存在于 .env（不进 git）。
早先版本把 jwt_secret 和数据库密码写成了代码默认值，等于把生产凭据提交进仓库；
现在改成空默认 + 启动时校验，缺了就直接起不来，而不是悄悄用一个已泄露的值。

## 两种部署形态

`MEM_MODE` 决定服务怎么跑：

- `server`（默认，服务器上用）：PostgreSQL + 上游账号体系 + nginx 反代。
  必填 MEM_DATABASE_URL / MEM_JWT_SECRET。

- `local`（本地机器用）：SQLite + 单用户免登录 + 只听 127.0.0.1。
  **零必填项** —— 什么都不配也能 `python -m app.local` 直接起来，
  数据落在 `~/.memorys/`。这是本地模式存在的全部意义：
  如果本地跑还要先装 PG、配 secret、建库，那不如直接连服务器。

local 模式下所有必填校验都关掉，缺失的值用安全的本地默认补上（见 _apply_local_defaults）。
"""
import os
import secrets
import sys
from pathlib import Path

from pydantic_settings import BaseSettings

# 项目根。原来写死 /opt/memorys/.env —— 本地模式下这个路径不存在，
# 用户 clone 到哪都得能跑，所以按本文件位置回推。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 本地模式的数据根。放 ~/.memorys 而不是项目目录：
#   1. 项目目录可能是只读的（系统级安装、容器挂载）
#   2. git pull / 重新 clone 不该碰到用户数据
#   3. 跟 ~/.ssh ~/.config 一个习惯，用户知道去哪找、去哪备份
LOCAL_HOME = Path(os.environ.get("MEM_LOCAL_HOME") or (Path.home() / ".memorys"))


class Settings(BaseSettings):
    # server | local
    mode: str = "server"

    # server 模式必填，local 模式自动填 sqlite
    database_url: str = ""
    data_dir: str = "/var/lib/memorys"
    # server 模式必填。与上游账号系统共享的 HS256 secret：本地签发的 JWT 带 iss=memorys；
    # 上游 token 无 iss，据此区分两条验证路径。
    jwt_secret: str = ""
    access_ttl_minutes: int = 720
    # 复用哪个站的账号体系做代理登录。local 模式留空 → 走单用户免登录
    upstream_base: str = ""
    github_remote: str = ""
    sync_branch: str = "main"
    sync_identity_name: str = "memorys-bot"
    sync_identity_email: str = "memorys@example.com"
    embed_api_base: str = ""
    embed_api_key: str = ""
    embed_model: str = "text-embedding-3-small"
    # 向量维度。必须和上游实际返回的一致 —— hnsw 索引建在固定维度上，
    # 写入维度不符会被 pgvector 直接拒。改这个值必须重建索引 + 全量 reindex。
    # 实测：阿里云百炼 qwen3.7-text-embedding-flash 返回 1024（给 1536 会被忽略）。
    embed_dim: int = 1536
    # 批量上限。阿里云百炼是 25，超一条整批 400。
    embed_batch_size: int = 25
    # 写入侧超时：reindex 是后台批处理，慢一点没关系，值得等重试。
    embed_timeout: float = 30.0
    # 查询侧总预算：检索是交互路径。上游实测会随机 ReadTimeout，
    # 不能让一次抖动把搜索卡住 —— 超了就直接降级成两路关键词检索，
    # 用户拿到的结果略差，但立刻有结果。
    embed_query_timeout: float = 4.0
    # 单条输入的截断长度（字符）。
    #
    # ⚠️ 这个值必须按**模型的 token 上限**倒推，不是随便定的容量保护。
    # 实测阿里云百炼 qwen3.7-text-embedding-flash 的上限是 **512 token**，
    # 而且超限时**不返回 400，而是直接挂住到 ReadTimeout** —— 从调用方看完全像
    # 网络抖动，实际是确定性的（同一条内容重试 3 次全部超时，截短 20 字立刻 200）。
    #
    # 中文约 1 token/字（实测 1019 字 = 503 token），英文约 4 字符/token。
    # 1000 字符对纯中文≈500 token，刚好卡在上限内；混排内容 token 更少，更安全。
    # 真正的守门在 embed_texts 的 _fit_tokens()，那里按字符类型估 token 再截。
    #
    # 换成 text-embedding-v4 可以放宽（实测 8000 字 = 4871 token 正常返回）。
    embed_max_chars: int = 1000
    # 单条输入的 token 预算。超了就按这个数截断，而不是让上游超时。
    # flash=512，v3/v4=8192。填错的代价：偏大 → 长 chunk 静默拿不到向量；
    # 偏小 → 白丢内容尾部，检索质量下降但不会报错。
    embed_max_tokens: int = 500
    app_host: str = "127.0.0.1"
    app_port: int = 8649
    # 反代挂载前缀。nginx 把 /mem/ strip 掉后转发到本服务，本服务自身路由不含前缀，
    # 但 /api/docs 页面内引用的 openapi.json 是绝对路径，必须靠 root_path 加回前缀，
    # 否则 Swagger UI 会去宿主站根目录取 openapi.json 而 404。
    # 直接挂域名根时置空。local 模式自动置空（没有反代）。
    root_path: str = "/mem"
    # MCP 的 DNS rebinding 防护白名单。后端只听 127.0.0.1，FastMCP 会据此自动只放行
    # 本机 Host；经 nginx 反代进来的 Host 是真实域名，必须在这里显式放行，否则 421。
    # 逗号分隔，支持 host:* 通配。
    mcp_allowed_hosts: str = "127.0.0.1:*,localhost:*,[::1]:*"

    # ---- local 模式专属 ----
    # 免登录的单用户名。local 模式下所有请求都归属这个用户，不校验凭据。
    local_user: str = "local"
    # 是否放开鉴权。默认 True —— 本地模式的价值就在于开箱即用，
    # 逼用户先建 API Key 再用等于把服务器模式的负担搬到本地。
    #
    # ⚠️ 安全边界：这意味着**任何能访问 app_host:app_port 的进程都是这个用户**。
    # 所以 local 模式强制只听 127.0.0.1（见 app/local.py 的 _guard_bind），
    # 想暴露到局域网必须显式设 MEM_LOCAL_OPEN=false 并建 API Key。
    local_open: bool = True

    model_config = {
        # env_file 在实例化时按模式覆盖（见下面 _env_file_for）。
        # 这里给的是 server 模式的默认值。
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_prefix": "MEM_",
        "extra": "ignore",
    }


def _mode_from_env() -> str:
    """在实例化 Settings 之前先探出 mode。

    绕这一圈是必须的：mode 决定该读哪个 .env，而读 .env 又要先有 Settings。
    所以这里只看真实环境变量 —— `python -m app.local` 和 systemd 单元
    都是通过环境变量设 MEM_MODE 的，不需要从文件里读。
    """
    return (os.environ.get("MEM_MODE") or "server").strip().lower()


def _env_file_for(mode: str) -> str:
    """local 模式**不读项目根的 .env**。

    这一条是安全边界，不是风格选择：项目根的 .env 里是生产 PG 连接串和
    生产 JWT secret（服务器上就是这么部署的）。如果本地模式也读它，
    那么在服务器上 clone 了代码、或者把 .env 带到本地的人，
    一执行 `python -m app.local` 就直接连上了生产库 ——
    本地随手测试的增删改会落到真实数据上，而界面上完全看不出区别。

    所以 local 模式只读 ~/.memorys/.env（用户自己的本地覆盖），
    项目 .env 一律忽略。想在本地连生产库排查问题，显式传
    MEM_DATABASE_URL=... 环境变量，那是有意识的动作。
    """
    if mode == "local":
        return str(LOCAL_HOME / ".env")
    return str(PROJECT_ROOT / ".env")


_mode = _mode_from_env()
if _mode == "local":
    # 读 local 的 env 文件前先保证目录存在，否则 pydantic-settings 读不到会静默跳过
    LOCAL_HOME.mkdir(parents=True, exist_ok=True)
settings = Settings(_env_file=_env_file_for(_mode))  # type: ignore[call-arg]
# 环境变量里没显式给 mode 时，Settings 的默认值是 "server"；
# 给了就用给的那个（上面已经据此选了 env 文件，这里保持一致）
settings.mode = _mode


def _apply_local_defaults(s: Settings) -> None:
    """local 模式的自动补全。

    原则：**不覆盖用户显式配置的值**，只补空的。
    有人会在本地也配自己的 PG 或自己的 embedding 端点，那些应该照用。
    """
    LOCAL_HOME.mkdir(parents=True, exist_ok=True)

    if not s.database_url:
        # aiosqlite 驱动 + 绝对路径（三斜杠后接绝对路径 = 四个斜杠）
        s.database_url = f"sqlite+aiosqlite:///{LOCAL_HOME / 'memorys.db'}"
    if s.data_dir == "/var/lib/memorys":
        # /var/lib 在本地机器上通常没有写权限，而且不该往系统目录塞用户数据
        s.data_dir = str(LOCAL_HOME / "data")
    if not s.jwt_secret:
        # 持久化到文件而不是每次随机：随机的话每次重启所有已签发 token 失效，
        # Web UI 会莫名其妙掉登录。600 权限，只有当前用户能读。
        f = LOCAL_HOME / "jwt_secret"
        if f.exists():
            s.jwt_secret = f.read_text(encoding="utf-8").strip()
        if not s.jwt_secret:
            s.jwt_secret = secrets.token_hex(32)
            f.write_text(s.jwt_secret, encoding="utf-8")
            try:
                f.chmod(0o600)
            except Exception:
                pass
    if s.root_path == "/mem":
        # 本地直连，没有反代前缀
        s.root_path = ""


IS_LOCAL = settings.mode.strip().lower() == "local"

if IS_LOCAL:
    _apply_local_defaults(settings)
else:
    # 启动即校验。宁可起不来也不要拿空 secret 签 JWT —— 空 secret 意味着任何人
    # 都能伪造出合法 token，而服务照样返回 200，问题要到数据被人拿走才会暴露。
    _missing = [k for k, v in (("MEM_DATABASE_URL", settings.database_url),
                               ("MEM_JWT_SECRET", settings.jwt_secret)) if not v]
    if _missing:
        sys.exit(
            f"启动中止：缺少必填配置 {', '.join(_missing)}。\n"
            f"请复制 .env.example 为 .env 并填写（详见 README「部署」节）。\n"
            f"或者想在本地跑：设 MEM_MODE=local，什么都不用配。"
        )
    if len(settings.jwt_secret) < 32:
        sys.exit("启动中止：MEM_JWT_SECRET 过短（至少 32 字符，用 openssl rand -hex 32 生成）。")

# 后端方言。放在这里而不是每次现算：dialect 的分支遍布 service 层，
# 每次解析连接串字符串是白开销，而且容易出现两处判断不一致。
from .dialect import backend_of  # noqa: E402  (循环导入：dialect 不 import config)

DB_BACKEND = backend_of(settings.database_url)
