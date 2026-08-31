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
