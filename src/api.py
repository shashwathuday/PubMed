"""FastAPI backend that exposes PubMed search and save functionality.

Endpoints:
- GET /health: basic health check
- POST /search: run an esearch + optional detail fetch, returning records
- POST /save: persist provided records to PostgreSQL

This reuses the existing pubmed_client and db modules to keep logic DRY.
"""

from __future__ import annotations

from typing import List, Optional, Any, Dict
import os

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.pubmed_client import esearch, esummary, fetch_with_abstracts, PubMedRecord
from src.db import save_records
from src.db import get_engine

app = FastAPI(title="PubMed API", version="0.1.0")

# Allow local UIs (Streamlit on 8501, dev tools, etc.) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchRequest(BaseModel):
    query: str = Field(..., description="PubMed query string")
    retmax: int = Field(50, ge=1, le=1000)
    mindate: Optional[str] = Field(None, description="YYYY/MM/DD")
    maxdate: Optional[str] = Field(None, description="YYYY/MM/DD")
    include_abstracts: bool = True
    # API key now only from environment (NCBI_EUTILS_API_KEY)


class RecordModel(BaseModel):
    pmid: str
    title: Optional[str] = None
    authors: List[str] = []
    journal: Optional[str] = None
    pubdate: Optional[str] = None
    doi: Optional[str] = None
    abstract: Optional[str] = None


class SaveRequest(BaseModel):
    records: List[RecordModel]


class SearchResponse(BaseModel):
    records: List[RecordModel]


class SaveResponse(BaseModel):
    saved: int


class QARequest(BaseModel):
    question: str
    top_k: int = 100
    model: Optional[str] = None


class QAResponse(BaseModel):
    sql: str
    rows: List[Dict[str, Any]]


def _build_sql_prompt(question: str) -> str:
    return f"""
You are a careful SQL assistant. Translate the user's request into a single safe SQL SELECT query.

Rules:
- Target table: articles
- Allowed columns: title (TEXT), journal (VARCHAR), pubdate (VARCHAR), doi (VARCHAR), pmid (TEXT)
- You may compute year from pubdate using SUBSTRING(pubdate, 1, 4) AS year
- Use ILIKE with wildcards for text filters.
- Never modify data; only SELECT. No semicolons.
- Always include a LIMIT, e.g., LIMIT 100.

User question:
{question}
"""


def _extract_sql_from_text(text: str) -> str:
    """Extract SQL from fenced code blocks if present; otherwise return stripped text."""
    import re
    m = re.search(r"```sql\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.*?)```", text, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _generate_sql_with_gemini(question: str, model: str, api_key: Optional[str]) -> str:
    import google.generativeai as genai
    key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY/GOOGLE_API_KEY is required for Q&A")
    genai.configure(api_key=key)

    resp = genai.GenerativeModel(model).generate_content(_build_sql_prompt(question))
    if getattr(resp, "prompt_feedback", None) and getattr(resp.prompt_feedback, "block_reason", None):
        raise RuntimeError(f"Gemini blocked the prompt: {resp.prompt_feedback.block_reason}")

    text = getattr(resp, "text", None)
    if not text:
        try:
            text = "".join(getattr(p, "text", "") for p in resp.candidates[0].content.parts)
        except Exception:
            text = ""
    if not text:
        raise RuntimeError("Gemini returned no text content")
    return _extract_sql_from_text(text)


@app.get("/models")
def list_gemini_models() -> Dict[str, List[str]]:
    """List available Gemini model IDs that support generateContent for the configured API key."""
    try:
        import google.generativeai as genai
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise HTTPException(status_code=400, detail="Set GEMINI_API_KEY or GOOGLE_API_KEY to list models")
        genai.configure(api_key=key)
        out: List[str] = []
        for m in genai.list_models():
            methods = getattr(m, "supported_generation_methods", []) or []
            if "generateContent" in methods:
                out.append(getattr(m, "name", ""))
        # The SDK returns fully-qualified names like models/gemini-2.5-flash-8b; normalize to the short id too
        short = sorted({n.split("/")[-1] for n in out})
        return {"models": short}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list models: {e}")


def _is_sql_safe(sql: str) -> bool:
    s = sql.strip().lower()
    if not s.startswith("select"):
        return False
    if ";" in s:
        return False
    banned = ("insert", "update", "delete", "drop", "alter", "truncate", "create", "grant", "revoke")
    return not any(tok in s for tok in banned)


def _serialize_records(records: List[PubMedRecord]) -> List[RecordModel]:
    return [
        RecordModel(
            pmid=r.pmid,
            title=r.title,
            authors=list(r.authors or []),
            journal=r.journal,
            pubdate=r.pubdate,
            doi=r.doi,
            abstract=r.abstract,
        )
        for r in records
    ]


@app.post("/qa", response_model=QAResponse, status_code=status.HTTP_200_OK)
def qa(req: QARequest):
    # Default to a fast Gemini model; allow override via env or request
    model = req.model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    # API key strictly from environment
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY (or GOOGLE_API_KEY) is required")

    try:
        sql = _generate_sql_with_gemini(req.question, model=model, api_key=api_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM error: {e}")

    # Enforce safety and add/cap LIMIT
    if not _is_sql_safe(sql):
        raise HTTPException(status_code=400, detail=f"Generated SQL rejected by safety checks: {sql}")
    import re
    m = re.search(r"\blimit\s+(\d+)\b", sql, flags=re.IGNORECASE)
    if m:
        # Cap existing LIMIT to top_k
        cap = int(req.top_k)
        sql = re.sub(r"\blimit\s+(\d+)\b", lambda mm: f"LIMIT {min(int(mm.group(1)), cap)}", sql, flags=re.IGNORECASE)
    else:
        # Append LIMIT top_k safely
        sql = f"{sql.rstrip()} LIMIT {int(req.top_k)}"

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise HTTPException(status_code=400, detail="DATABASE_URL is required")
    try:
        engine = get_engine(db_url)
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            rows = [dict(r._mapping) for r in result.fetchall()]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"SQL execution error: {e}")

    return QAResponse(sql=sql, rows=rows)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/search", response_model=SearchResponse, status_code=status.HTTP_200_OK)
async def search(req: SearchRequest):
    if not req.query:
        raise HTTPException(status_code=400, detail="query is required")

    ncbi_key = os.getenv("NCBI_EUTILS_API_KEY")
    pmids = esearch(
        req.query,
        api_key=ncbi_key,
        retmax=req.retmax,
        mindate=req.mindate,
        maxdate=req.maxdate,
    )

    if not pmids:
        return SearchResponse(records=[])

    if req.include_abstracts:
        records = fetch_with_abstracts(pmids, api_key=ncbi_key)
    else:
        records = esummary(pmids, api_key=ncbi_key)

    # Convert dataclass to vanilla dicts for JSON response
    return SearchResponse(records=_serialize_records(records))


@app.post("/save", response_model=SaveResponse, status_code=status.HTTP_200_OK)
async def save(req: SaveRequest):
    # Convert Pydantic models back into PubMedRecord dataclasses
    recs = [
        PubMedRecord(
            pmid=r.pmid,
            title=r.title,
            authors=r.authors or [],
            journal=r.journal,
            pubdate=r.pubdate,
            doi=r.doi,
            abstract=r.abstract,
        )
        for r in req.records
    ]

    try:
        saved = save_records(recs, database_url=None)
        return SaveResponse(saved=saved)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
