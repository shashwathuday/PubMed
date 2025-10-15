"""Thin client for NCBI E-utilities (PubMed) with simple rate-limiting.

This module wraps common E-utilities endpoints used in PubMed workflows:
- esearch: search for article PMIDs matching a query.
- esummary: fetch article metadata for PMIDs.
- efetch (XML): fetch article abstracts and combine with esummary metadata.

It intentionally avoids heavy dependencies and keeps responses mapped into a
simple dataclass structure that the UI can consume.
"""

from dataclasses import dataclass
import time
import requests

# Base URL for all Entrez E-utilities endpoints
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Conservative sleep durations to respect NCBI rate limits
SLEEP_WITH_KEY = 0.11  # ~9 req/s
SLEEP_NO_KEY = 0.35    # ~3 req/s


@dataclass
class PubMedRecord:
    """Normalized representation of a PubMed article.

    Fields are a subset commonly used in quick search UIs and exports.
    Some fields can be None if data is unavailable from the chosen endpoint.
    """

    pmid: str  # PubMed ID
    title: str | None  # Article title
    authors: list[str]  # Author names in display order
    journal: str | None  # Full journal name when available
    pubdate: str | None  # Publication date string returned by API
    doi: str | None  # First DOI found among article IDs
    abstract: str | None  # Abstract text (None if not fetched)


def _rate_limit_sleep(api_key: str | None) -> None:
    """Sleep a short time to respect NCBI rate limits.

    NCBI guidance (roughly):
      - With an API key: up to ~10 requests/second
      - Without a key: ~3 requests/second

    We add a conservative delay after each call.
    """

    time.sleep(SLEEP_WITH_KEY if api_key else SLEEP_NO_KEY)


def esearch(
    query: str,
    api_key: str | None = None,
    retmax: int = 100,
    mindate: str | None = None,
    maxdate: str | None = None,
    sort: str = "relevance",
) -> list[str]:
    """Search PubMed and return a list of PMIDs matching the query.

    Parameters
    - query: Entrez/PubMed search string, e.g. "(LLM) AND (systematic review)".
    - api_key: Optional NCBI API key to increase rate limits.
    - retmax: Maximum number of PMIDs to return.
    - mindate/maxdate: Date filters (when provided, we set datetype=pdat).
    - sort: One of PubMed-supported sorts, e.g. relevance, pub date.
    """

    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": retmax,
        "sort": sort,
    }

    # If either date bound is provided, configure date filtering by publication date (pdat)
    if mindate or maxdate:
        params.update({
            "mindate": mindate or "1800",
            "maxdate": maxdate or "3000",
            "datetype": "pdat",
        })

    if api_key:
        params["api_key"] = api_key

    # Perform search request
    r = requests.get(f"{EUTILS_BASE}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    _rate_limit_sleep(api_key)

    # Extract PMID list from JSON response structure
    return data.get("esearchresult", {}).get("idlist", [])


def efetch_pmids(pmids: list[str], api_key: str | None = None) -> list[PubMedRecord]:
    """Compatibility wrapper for efetch that delegates to esummary for metadata.

    Historically efetch with retmode=json is limited for metadata. We call
    esummary() to return a uniform PubMedRecord list. If you need abstracts,
    use fetch_with_abstracts() which also parses efetch XML.
    """

    if not pmids:
        return []

    # A real efetch(json) call would go here; we keep the rate-limit cadence
    # similar and then return esummary for better consistency.
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
        "rettype": "abstract",
    }
    if api_key:
        params["api_key"] = api_key

    r = requests.get(f"{EUTILS_BASE}/efetch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    _rate_limit_sleep(api_key)

    # The JSON retmode for efetch lacks rich fields; esummary provides them.
    return esummary(pmids, api_key)


def esummary(pmids: list[str], api_key: str | None = None) -> list[PubMedRecord]:
    """Fetch summary metadata (title, authors, journal, pubdate, doi) for PMIDs.

    Note: esummary does not include abstracts; see fetch_with_abstracts() if needed.
    """

    if not pmids:
        return []

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
    }
    if api_key:
        params["api_key"] = api_key

    r = requests.get(f"{EUTILS_BASE}/esummary.fcgi", params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    _rate_limit_sleep(api_key)

    results: list[PubMedRecord] = []
    uidmap = data.get("result", {})
    uids = uidmap.get("uids", [])  # list of PMID strings

    for uid in uids:
        item = uidmap.get(uid, {})
        title = item.get("title")

        # Extract display author names when present
        authors = [a.get("name") for a in item.get("authors", []) if a.get("name")]

        # Prefer full journal name; fallback to source
        journal = item.get("fulljournalname") or item.get("source")
        pubdate = item.get("pubdate")

        # Find first DOI in article IDs
        doi = None
        for articleid in item.get("articleids", []) or []:
            if articleid.get("idtype") == "doi":
                doi = articleid.get("value")
                break

        # esummary does not provide abstracts; keep None (or minimal placeholder)
        abstract = None

        results.append(
            PubMedRecord(
                pmid=uid,
                title=title,
                authors=authors,
                journal=journal,
                pubdate=pubdate,
                doi=doi,
                abstract=abstract,
            )
        )

    return results


def fetch_with_abstracts(pmids: list[str], api_key: str | None = None) -> list[PubMedRecord]:
    """Fetch metadata (via esummary) and abstracts (via efetch XML) for PMIDs.

    This performs two calls:
      1) esummary for normalized metadata
      2) efetch with retmode=xml to parse <AbstractText> segments
    The two are merged by PMID.
    """

    # Combine esummary for metadata + efetch rettype=abstract in XML to parse abstracts
    # To keep deps light, do a simple XML parse here using the stdlib.
    import xml.etree.ElementTree as ET

    if not pmids:
        return []

    # First get metadata via esummary (title/authors/journal/pubdate/doi)
    meta = {r.pmid: r for r in esummary(pmids, api_key)}

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    }
    if api_key:
        params["api_key"] = api_key

    r = requests.get(f"{EUTILS_BASE}/efetch.fcgi", params=params, timeout=90)
    r.raise_for_status()
    _rate_limit_sleep(api_key)

    # Parse XML for <AbstractText> nodes; preserve labeled sections when present
    root = ET.fromstring(r.text)
    ns: dict[str, str] = {}
    records: list[PubMedRecord] = []

    for article in root.findall('.//PubmedArticle', ns):
        pmid_el = article.find('.//PMID', ns)
        pmid = pmid_el.text if pmid_el is not None else None
        if not pmid:
            # Skip entries without a PMID (unexpected but defensive)
            continue

        # Gather all abstract fragments, keeping labels if provided (e.g., Background, Methods)
        abstract_texts: list[str] = []
        for ab in article.findall('.//AbstractText', ns):
            label = ab.attrib.get('Label')
            text = ''.join(ab.itertext()).strip()
            if label:
                abstract_texts.append(f"{label}: {text}")
            else:
                abstract_texts.append(text)
        abstract = '\n'.join(abstract_texts) if abstract_texts else None

        # Merge with esummary metadata when available; otherwise keep whatever we parsed
        m = meta.get(pmid)
        if m:
            records.append(
                PubMedRecord(
                    pmid=pmid,
                    title=m.title,
                    authors=m.authors,
                    journal=m.journal,
                    pubdate=m.pubdate,
                    doi=m.doi,
                    abstract=abstract,
                )
            )
        else:
            records.append(
                PubMedRecord(
                    pmid=pmid,
                    title=None,
                    authors=[],
                    journal=None,
                    pubdate=None,
                    doi=None,
                    abstract=abstract,
                )
            )

    return records
