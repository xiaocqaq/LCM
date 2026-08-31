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
    embed_dim: int = 1536
    app_host: str = "127.0.0.1"
    app_port: int = 8649
    # MCP 的 DNS rebinding 防护白名单。后端只听 127.0.0.1，FastMCP 会据此自动只放行
    # 本机 Host；经 nginx 反代进来的 Host 是真实域名，必须在这里显式放行，否则 421。
    # 逗号分隔，支持 host:* 通配。
    mcp_allowed_hosts: str = "127.0.0.1:*,localhost:*,[::1]:*,utils.xlingo.fun,mem.xlingo.fun"

    model_config = {"env_file": "/opt/memorys/.env", "env_prefix": "MEM_", "extra": "ignore"}


settings = Settings()
