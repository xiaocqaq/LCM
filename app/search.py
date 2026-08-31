"""检索：jieba 中文分词 → tsvector(OR 语法) 关键词检索；pg_trgm 相似度兜底；可选 embedding 混合(RRF)。
tokenize 是双向桥：写路径用它生成 lexeme 存库，读路径用它解析查询词。
"""
import re

import jieba

from .config import settings

jieba.setLogLevel(60)
_STOP = {"的", "了", "和", "是", "在", "我", "有", "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "那", "与", "及", "或", "等", "对", "为", "中", "用", "怎么", "如何", "什么", "which", "the", "and", "for", "how", "what"}

_WORD_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    text = re.sub(r"[#*`>\[\]()!:_\-—|/\\\"'.,;?!~@$%^&+=<>{}]", " ", text or "")
    words = [w.strip().lower() for w in jieba.cut_for_search(text)]
    out = []
    for w in words:
        w = _WORD_RE.sub("", w)
        if len(w) > 1 and w not in _STOP and w not in out:
            out.append(w)
        if len(out) >= 64:
            break
    return out


def to_tsquery(text: str) -> str:
    words = tokenize(text)
    return " | ".join(words) if words else ""


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
