"""Markdown + YAML frontmatter 读写。md 文件是 source of truth，DB 只是索引。"""
import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

VALID_TYPES = {"project_summary", "decision", "preference", "howto", "glossary", "fact"}

FM_KEYS = ["id", "title", "type", "project", "tags", "importance", "source", "created_at", "updated_at"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_slug(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip()
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", s)
    s = re.sub(r"\s+", "-", s).strip("-.")
    s = s[:100] or "untitled"
    return s


def build_frontmatter(meta: dict) -> str:
    ordered = {}
    for k in FM_KEYS:
        if k in meta and meta[k] not in (None, "", []):
            ordered[k] = meta[k]
    return "---\n" + yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, default_flow_style=False) + "---\n"


def parse_document(raw: str) -> tuple[dict, str]:
    """返回 (meta, content)。缺 frontmatter 时 meta={}。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", raw, re.S)
    if not m:
        return {}, raw
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, raw[m.end():]


def compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def user_root(data_dir: str, user_id: int) -> Path:
    return Path(data_dir) / "data" / "users" / f"u{user_id}"


def doc_path(data_dir: str, user_id: int, library: str, slug: str) -> Path:
    lib = sanitize_slug(library) or "main"
    return user_root(data_dir, user_id) / lib / f"{sanitize_slug(slug)}.md"


def render_document(meta: dict, content: str) -> str:
    return build_frontmatter(meta) + "\n" + content.rstrip() + "\n"


def split_chunks(content: str, max_chars: int = 1600) -> list[dict]:
    """按标题层级切块；单块超长再按段落二次切分。"""
    lines = content.split("\n")
    chunks: list[dict] = []
    cur: dict = {"heading": "", "lines": []}

    def flush():
        text = "\n".join(cur["lines"]).strip()
        if text:
            chunks.append({"heading": cur["heading"], "content": text})

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m and m.group(1) in ("#", "##", "###"):
            flush()
            cur = {"heading": m.group(2).strip()[:256], "lines": [line]}
        else:
            cur["lines"].append(line)
            if sum(len(x) + 1 for x in cur["lines"]) > max_chars:
                flush()
                cur = {"heading": cur["heading"], "lines": []}
    flush()
    return chunks
