"""数据模型。md 文件是 source of truth，DB 只做索引与元数据。"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    xiaoai_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)  # ai.xlingo.fun identity_users.id
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    email: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(32), default="user")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64), default="")
    prefix: Mapped[str] = mapped_column(String(16), default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    library: Mapped[str] = mapped_column(String(64), default="main")
    slug: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(256))
    md_type: Mapped[str] = mapped_column(String(32), default="fact")  # project_summary/decision/preference/howto/glossary/fact
    project: Mapped[str] = mapped_column(String(128), default="")
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    importance: Mapped[int] = mapped_column(Integer, default=3)
    source: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text, default="")  # frontmatter 之后的正文
    # 文档间关系。借自 graph-memory 的边模型，但只保留能改变检索行为的那几种，
    # 且存在 frontmatter 里（md 仍是 source of truth，图关系不另立数据源）。
    # 形如 [{"type": "supersedes", "target": "slug-or-title", "note": "为什么"}]
    links: Mapped[list] = mapped_column(JSONB, default=list)
    # 被别的文档 supersedes 时置位。检索与 bootstrap 默认跳过，
    # 但文件还在、还能直接读 —— 「过时」不等于「删除」。
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 实际被取用的次数。手填的 importance 区分度会退化（实测 96% 的文档都填了 ≥4），
    # 真正被反复读到的才是有用的知识。graph-memory 用 validatedCount 做同一件事。
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # frontmatter 扩展字段原样保留
    rel_path: Mapped[str] = mapped_column(String(300))  # 相对用户根目录，如 main/xxx.md
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("ux_documents_user_path", "user_id", "rel_path", unique=True),
        Index("idx_documents_user_alive", "user_id", "deleted_at"),
    )


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    heading: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    tsv: Mapped[object] = mapped_column(TSVECTOR, nullable=True)
    embedding: Mapped[object] = mapped_column(Text, nullable=True)  # 建为 text，启动时 ALTER 成 vector(dim)
    __table_args__ = (
        Index("idx_chunks_doc", "document_id", "seq"),
    )
