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


async def embed_query(text_: str) -> list[float] | None:
    """给检索用的单条嵌入，有硬性时间预算。

    和 embed_texts 分开是因为两者对"慢"的容忍度完全不同：
    reindex 是后台批处理，等重试是对的；检索是交互路径，宁可降级也不能卡住。
    """
    import asyncio

    if not EMBED_AVAILABLE:
        return None
    try:
        vecs = await asyncio.wait_for(
            embed_texts([text_], timeout=settings.embed_query_timeout, retries=0),
            timeout=settings.embed_query_timeout + 0.5,
        )
    except (TimeoutError, asyncio.TimeoutError):
        return None
    except Exception:
        return None
    if not vecs or not vecs[0]:
        return None
    return vecs[0]


async def embed_texts(texts: list[str], timeout: float | None = None,
                      retries: int = 1) -> list[list[float]] | None:
    """OpenAI 兼容 /embeddings。未配置或失败返回 None（检索自动降级为纯关键词）。

    分批提交：上游有批量上限（阿里云百炼 25，超一条整批 400），
    一次 reindex 的 chunk 数远超这个值。

    重试策略是实测逼出来的：阿里云会**随机** ReadTimeout，跟内容、长度、批量都无关
    （同一条 chunk 单独重发也可能超时，而更长的下一条却正常）。所以：
      1. 整批失败先原样重试
      2. 仍失败就拆成单条逐个要 —— 一批 25 条里通常只有 1-2 条踩雷，
         拆开后其余 23 条能正常拿到，比整批放弃划算得多
      3. 单条也失败才认输

    返回 None 表示"这批一个都没成"，调用方降级为纯关键词。
    部分成功用 None 占位而不是丢弃，让调用方知道哪几条缺向量。
    """
    if not EMBED_AVAILABLE or not texts:
        return None
    import asyncio

    import httpx

    base = settings.embed_api_base.rstrip("/")
    if not base.endswith("/embeddings"):
        base += "/embeddings"
    bs = max(1, settings.embed_batch_size)
    payload_extra: dict = {}
    if settings.embed_dim:
        # 支持 Matryoshka 截断的模型（text-embedding-v3/v4、qwen3.x）认这个参数；
        # 不支持的会忽略，所以不能靠它保证维度，仍要在写入侧校验
        payload_extra["dimensions"] = settings.embed_dim

    async def _post(client, batch: list[str]) -> list[list[float]]:
        resp = await client.post(
            base,
            headers={"Authorization": f"Bearer {settings.embed_api_key}"},
            json={
                "model": settings.embed_model,
                "input": batch,
                "encoding_format": "float",
                **payload_extra,
            },
        )
        resp.raise_for_status()
        rows = resp.json()["data"]
        # 上游不保证顺序，按 index 归位
        rows.sort(key=lambda d: d.get("index", 0))
        vecs = [d["embedding"] for d in rows]
        if len(vecs) != len(batch):
            raise ValueError(f"上游返回 {len(vecs)} 条，期望 {len(batch)}")
        return vecs

    out: list[list[float] | None] = []
    async with httpx.AsyncClient(timeout=timeout or settings.embed_timeout) as client:
        for i in range(0, len(texts), bs):
            batch = [t[: settings.embed_max_chars] for t in texts[i : i + bs]]
            got: list[list[float]] | None = None
            for attempt in range(retries + 1):
                try:
                    got = await _post(client, batch)
                    break
                except Exception:
                    if attempt < retries:
                        await asyncio.sleep(1.5)
            if got is not None:
                out.extend(got)
                continue
            if retries == 0 or len(batch) == 1:
                # 查询路径（retries=0）不做逐条降级：只有一条，拆也没意义，
                # 且再等一轮就超出交互预算了
                out.extend([None] * len(batch))
                continue
            # 整批重试仍失败 → 拆单条捞回大部分（一批里通常只有 1-2 条踩雷）
            for t in batch:
                one: list[float] | None = None
                for attempt in range(2):
                    try:
                        one = (await _post(client, [t]))[0]
                        break
                    except Exception:
                        if attempt == 0:
                            await asyncio.sleep(1.0)
                out.append(one)

    if not any(v is not None for v in out):
        return None
    return out  # type: ignore[return-value]
