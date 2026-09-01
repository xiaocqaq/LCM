"""配置。环境变量前缀 MEM_，默认读 /opt/memorys/.env"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = ""
    data_dir: str = "/var/lib/memorys"
    # 与 xiaoai-chat 相同的 HS256 secret：本地签发的 JWT 带 iss=memorys；
    # xiaoai 的 token 无 iss，据此区分两条验证路径
    jwt_secret: str = ""
    access_ttl_minutes: int = 720
    upstream_base: str = "https://ai.xlingo.fun"
    github_remote: str = ""
    sync_branch: str = "main"
    sync_identity_name: str = "memorys-bot"
    sync_identity_email: str = "memorys@xlingo.fun"
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
    embed_max_chars: int = 2500
    app_host: str = "127.0.0.1"
    app_port: int = 8649
    # 反代挂载前缀。nginx 把 /mem/ strip 掉后转发到本服务，本服务自身路由不含前缀，
    # 但 /api/docs 页面内引用的 openapi.json 是绝对路径，必须靠 root_path 加回前缀，
    # 否则 Swagger UI 会去宿主站根目录取 openapi.json 而 404。
    # 直接挂域名根（如 mem.xlingo.fun）时置空。
    root_path: str = "/mem"
    # MCP 的 DNS rebinding 防护白名单。后端只听 127.0.0.1，FastMCP 会据此自动只放行
    # 本机 Host；经 nginx 反代进来的 Host 是真实域名，必须在这里显式放行，否则 421。
    # 逗号分隔，支持 host:* 通配。
    mcp_allowed_hosts: str = "127.0.0.1:*,localhost:*,[::1]:*,utils.xlingo.fun,mem.xlingo.fun"

    model_config = {"env_file": "/opt/memorys/.env", "env_prefix": "MEM_", "extra": "ignore"}


settings = Settings()
