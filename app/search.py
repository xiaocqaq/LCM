"""检索：jieba 中文分词 → tsvector 关键词检索；pg_trgm 相似度兜底；可选 embedding 混合(RRF)。

两侧不对称，是刻意的：
  写路径 expand_tokens()：原文分词 + 路径段 + 概念同义词，把索引"摊开"
  读路径 tokenize()/to_tsquery()：只用用户真实输入的词，不膨胀
这样"记忆文件存在哪个目录"能命中正文里只写了 /var/lib/... 的 chunk，
同时不会因为查询扩展而把无关文档拉进结果。
"""
import re

import jieba

from .config import settings

jieba.setLogLevel(60)
_STOP = {"的", "了", "和", "是", "在", "我", "有", "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "那", "与", "及", "或", "等", "对", "为", "中", "用", "怎么", "如何", "什么", "which", "the", "and", "for", "how", "what"}

_WORD_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]")


def tokenize(text: str, limit: int = 64) -> list[str]:
    text = re.sub(r"[#*`>\[\]()!:_\-—|/\\\"'.,;?!~@$%^&+=<>{}]", " ", text or "")
    words = [w.strip().lower() for w in jieba.cut_for_search(text)]
    out = []
    seen = set()
    for w in words:
        w = _WORD_RE.sub("", w)
        if len(w) > 1 and w not in _STOP and w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= limit:
            break
    return out


def to_tsquery(text: str) -> str:
    words = tokenize(text)
    return " | ".join(words) if words else ""


# ---- 索引侧同义扩展 ----
# 为什么只扩索引不扩查询：正文常写具体形态（路径 /var/lib/...、端口号、命令），
# 而人提问用概念词（"目录""文件""怎么配"）。把概念词注入索引，查询侧保持原样，
# 既补了召回，又不会因为查询膨胀而把无关文档拉进来。
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "目录": ("路径", "文件夹", "存放", "位置"),
    "路径": ("目录", "位置"),
    "文件": ("文档", "存放"),
    "端口": ("监听", "地址"),
    "监听": ("端口",),
    "部署": ("上线", "发版", "安装"),
    "上线": ("部署", "发布"),
    "配置": ("设置", "参数"),
    "鉴权": ("认证", "登录", "密钥", "token"),
    "认证": ("鉴权", "登录"),
    "备份": ("恢复", "回滚", "快照"),
    "数据库": ("db", "postgres", "pg"),
    "检索": ("搜索", "查询"),
    "记忆": ("知识", "文档", "笔记"),
}

# 路径/URL 里的通用噪声段，切出来当词没有区分度
_PATH_NOISE = {"var", "lib", "opt", "usr", "etc", "root", "home", "www", "srv",
               "tmp", "http", "https", "com", "cn", "fun", "localhost"}

# 英文技术词 → 中文概念词。正文常是英文（data/users/port），提问常是中文。
_EN2ZH: dict[str, tuple[str, ...]] = {
    "data": ("数据",),
    "users": ("用户",),
    "user": ("用户",),
    "config": ("配置",),
    "conf": ("配置",),
    "port": ("端口",),
    "log": ("日志",),
    "logs": ("日志",),
    "backup": ("备份",),
    "auth": ("鉴权", "认证"),
    "key": ("密钥", "鉴权"),
    "token": ("令牌", "鉴权"),
    "search": ("检索", "搜索"),
    "deploy": ("部署", "上线"),
    "service": ("服务",),
    "systemd": ("服务", "开机自启"),
    "nginx": ("反代", "网关"),
    "postgres": ("数据库",),
    "pg": ("数据库",),
    "db": ("数据库",),
    "git": ("版本", "提交"),
    "repo": ("仓库",),
    "mcp": ("工具", "接入"),
    "api": ("接口",),
    "rest": ("接口",),
    "md": ("markdown", "文档"),
}


def _path_terms(text: str) -> list[str]:
    """把 /var/lib/memorys/data/users 这类路径切成可检索的段，并标记它是个路径。"""
    out: list[str] = []
    if re.search(r"(/[\w.\-]+){2,}", text or ""):
        # 正文里出现了真实路径 → 让"目录/路径/文件夹"这类概念词能命中
        out.extend(["目录", "路径", "文件夹", "位置"])
    for seg in re.findall(r"[/\\]([A-Za-z0-9_.\-]{2,})", text or ""):
        seg = seg.lower().strip("._-")
        if len(seg) > 1 and seg not in _PATH_NOISE:
            out.append(seg)
    return out


def expand_tokens(text: str, limit: int = 220) -> list[str]:
    """索引侧用：原文分词 + 路径段 + 概念同义词，去重后返回。

    limit 比查询侧的 64 大得多：chunk 正文比查询长，且扩展词要挤得进来。
    """
    base = tokenize(text, limit=limit)
    out = list(base)
    seen = set(out)

    def add(w: str) -> None:
        if w and w not in seen:
            seen.add(w)
            out.append(w)

    # 路径段本身也要参与同义映射（data/users 是从路径里切出来的）
    path_terms = _path_terms(text)
    for extra in path_terms:
        add(extra)
    for w in list(base) + path_terms:
        for syn in _SYNONYMS.get(w, ()):
            add(syn)
        for syn in _EN2ZH.get(w, ()):
            add(syn)
    return out


EMBED_AVAILABLE = bool(settings.embed_api_base and settings.embed_api_key)


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """OpenAI 兼容 /embeddings。未配置或失败返回 None（检索自动降级为纯关键词）。"""
    if not EMBED_AVAILABLE or not texts:
        return None
    import httpx

    base = settings.embed_api_base.rstrip("/")
    if not base.endswith("/embeddings"):
        base += "/embeddings"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                base,
                headers={"Authorization": f"Bearer {settings.embed_api_key}"},
                json={"model": settings.embed_model, "input": [t[:3000] for t in texts]},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            return [d["embedding"] for d in data]
    except Exception:
        return None
