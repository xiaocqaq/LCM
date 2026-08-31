"""上游认证：复用 ai.xlingo.fun (xiaoai-chat) 的登录接口。
真实密码校验、锁定、2FA、审计全部在上游完成，本地不存密码。
"""
import httpx

from .config import settings


class UpstreamAuthError(Exception):
    def __init__(self, kind: str, message: str, status: int = 401):
        self.kind = kind  # invalid_credentials | two_factor | upstream_error
        self.message = message
        self.status = status
        super().__init__(message)


async def upstream_login(username: str, password: str) -> dict:
    """POST /api/v1/auth/login。成功返回 data（含 accessToken/user）；2FA 抛特殊错误。"""
    url = settings.upstream_base.rstrip("/") + "/api/v1/auth/login"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={"username": username, "password": password})
    except httpx.HTTPError as e:
        raise UpstreamAuthError("upstream_error", f"认证上游不可达: {e}", status=502)
    if resp.status_code == 401:
        raise UpstreamAuthError("invalid_credentials", "用户名或密码错误")
    if resp.status_code == 400:
        # 上游字段校验失败（如密码短于 6 位）。透传上游提示，别报成 502 误导用户。
        try:
            msg = (resp.json() or {}).get("errorMsg") or "请求参数不合法"
        except Exception:
            msg = "请求参数不合法"
        raise UpstreamAuthError("invalid_request", msg, status=400)
    if resp.status_code != 200:
        raise UpstreamAuthError("upstream_error", f"上游返回 {resp.status_code}", status=502)
    body = resp.json()
    data = body.get("data") or {}
    if data.get("twoFactorRequired"):
        raise UpstreamAuthError("two_factor", "该账号开启了两步验证，请在 ai.xlingo.fun 完成登录后用 accessToken 方式绑定")
    token = data.get("accessToken") or data.get("access_token")
    if not token:
        raise UpstreamAuthError("upstream_error", "上游响应缺少 accessToken", status=502)
    return data


async def upstream_me(access_token: str) -> dict:
    """GET /api/v1/me（Bearer）。返回 user 视图；失败抛 UpstreamAuthError。"""
    url = settings.upstream_base.rstrip("/") + "/api/v1/me"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as e:
        raise UpstreamAuthError("upstream_error", f"认证上游不可达: {e}", status=502)
    if resp.status_code != 200:
        raise UpstreamAuthError("invalid_credentials", "accessToken 无效或已过期")
    data = (resp.json() or {}).get("data") or {}
    if not data.get("id"):
        raise UpstreamAuthError("upstream_error", "上游 /me 响应异常", status=502)
    return data
