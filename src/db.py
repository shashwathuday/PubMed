"""PostgreSQL integration using SQLAlchemy 2.x.

This module provides:
- engine/session management reading DATABASE_URL from env (or .env via python-dotenv)
- a simple ORM model for PubMed articles
- helpers to upsert article records in bulk

Expected DATABASE_URL format for psycopg3:
  postgresql+psycopg://user:password@host:5432/dbname
"""

from __future__ import annotations

import os
from typing import Iterable, TYPE_CHECKING

from sqlalchemy import (
    create_engine,
    String,
    Text,
    Integer,
    DateTime,
    func,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
from sqlalchemy.dialects.postgresql import ARRAY

try:
    # Load environment variables from a .env file if present
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # Optional dependency; ignore if not installed
    pass


class Base(DeclarativeBase):
    """Base class for ORM models."""
    pass


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("pmid", name="uq_articles_pmid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pmid: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    authors: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    journal: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pubdate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(256), nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def get_engine(url: str | None = None):
    """Create and return a SQLAlchemy Engine.

    Uses the provided URL when given; otherwise reads DATABASE_URL from env.
    If not set, raises a clear error describing the expected format.
    """

    url = url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Expected format: postgresql+psycopg://user:pass@host:5432/dbname"
        )
    # echo=False to avoid noisy logs; set to True to debug SQL
    return create_engine(url, echo=False, future=True)


def init_db(url: str | None = None) -> None:
    """Create tables if they don't exist."""

    engine = get_engine(url)
    Base.metadata.create_all(engine)


if TYPE_CHECKING:
    # Only used for type hints; avoids runtime import cycles
    from src.pubmed_client import PubMedRecord  # noqa: F401


def upsert_articles(session: Session, records: Iterable["PubMedRecord"]) -> int:
    """Insert or update articles by PMID.

    Uses a simple pattern: try to find existing by PMID; update or insert accordingly.
    For larger volumes, consider COPY or bulk upsert patterns.
    Returns the number of upserted rows.
    """

    from src.pubmed_client import PubMedRecord  # import locally to avoid circulars

    count = 0
    for rec in records:
        # Find existing by PMID
        obj = session.query(Article).filter(Article.pmid == rec.pmid).one_or_none()
        if obj is None:
            obj = Article(
                pmid=rec.pmid,
                title=rec.title,
                authors=list(rec.authors or []),
                journal=rec.journal,
                pubdate=rec.pubdate,
                doi=rec.doi,
                abstract=rec.abstract,
            )
            session.add(obj)
        else:
            obj.title = rec.title
            obj.authors = list(rec.authors or [])
            obj.journal = rec.journal
            obj.pubdate = rec.pubdate
            obj.doi = rec.doi
            obj.abstract = rec.abstract
        count += 1
    return count


def save_records(records: Iterable["PubMedRecord"], database_url: str | None = None) -> int:
    """Initialize tables if needed and upsert all records in a single transaction.

    Parameters
    - records: iterable of PubMedRecord objects
    - database_url: optional URL to override env var (useful from UI input)

    Returns the number of records processed.
    """

    engine = get_engine(database_url)
    # Ensure tables exist before writing
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with SessionLocal() as session:
        count = upsert_articles(session, records)
        session.commit()
        return count
