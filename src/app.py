"""Streamlit UI for searching PubMed via a FastAPI backend and exporting results.

Flow:
 1) User inputs a query and optional date range in the sidebar.
 2) The app calls the FastAPI /search endpoint to retrieve records.
 3) Display results in a table and provide CSV/JSON downloads.
 4) Optionally call /save to persist results to PostgreSQL.
"""

import os
import sys
import typing as t
from pathlib import Path

import requests
import streamlit as st
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# (Optional) Ensure project root is on sys.path; harmless if unused
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Configure the Streamlit page (title, favicon, layout)
st.set_page_config(page_title="PubMed Search", page_icon="🔎", layout="wide")

# Main title displayed at the top of the page
st.title("🔎 PubMed Search UI")
tab_search, tab_qa = st.tabs(["Search", "Q&A"])

# Configuration from environment (no UI prompts for secrets)
API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")

# Sidebar collects all input controls and action button
with st.sidebar:
    st.header("Search")

    # Core query input: supports complex PubMed queries
    query = st.text_input("Query", placeholder="e.g. (large language model) AND (systematic review)")

    # Optional date filters (YYYY/MM/DD). If left blank, no date filtering is applied.
    col_a, col_b = st.columns(2)
    with col_a:
        mindate = st.text_input("From (YYYY/MM/DD)", placeholder="2018/01/01")
    with col_b:
        maxdate = st.text_input("To (YYYY/MM/DD)", placeholder="2025/10/13")

    # Maximum number of results to retrieve via esearch
    retmax = st.slider("Max results", 10, 500, 50, step=10)

    # Toggle to include abstracts (slower) or skip them (faster)
    include_abstracts = st.checkbox("Include abstracts", value=True)

    # Optional: Save results to a PostgreSQL database (DATABASE_URL from env)
    save_to_db = st.checkbox("Save results to PostgreSQL", value=False)

    # Execute the search
    run = st.button("Search", type="primary", use_container_width=True)

with tab_search:
    if run:
        # Validate that a query was provided
        if not query:
            st.warning("Please enter a query.")
            st.stop()

        # Step 1: Ask FastAPI to perform the search and return records
        with st.spinner("Searching via API..."):
            try:
                payload = {
                    "query": query,
                    "retmax": retmax,
                    "mindate": mindate or None,
                    "maxdate": maxdate or None,
                    "include_abstracts": include_abstracts,
                }
                resp = requests.post(f"{API_BASE}/search", json=payload, timeout=120)
                if resp.status_code != 200:
                    raise RuntimeError(f"API error {resp.status_code}: {resp.text}")
                data = resp.json()
                records = data.get("records", [])
            except Exception as e:
                st.error(f"API request failed: {e}")
                st.stop()

        st.caption(f"Found {len(records)} records")

        if not records:
            # Nothing to fetch/display
            st.info("No results.")
            st.stop()

        # Step 3: Display results
        import pandas as pd

        # records are dicts already from API, normalize and display
        df = pd.DataFrame([
            {
                "PMID": r.get("pmid"),
                "Title": r.get("title"),
                "Authors": "; ".join(r.get("authors", [])),
                "Journal": r.get("journal"),
                "PubDate": r.get("pubdate"),
                "DOI": r.get("doi"),
                "Abstract": r.get("abstract"),
            }
            for r in records
        ])

        # Interactive table with column sorting and horizontal scrolling
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Step 4: Download buttons for CSV and JSON
        st.subheader("Export")
        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8"),
            file_name="pubmed_results.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download JSON",
            df.to_json(orient="records", force_ascii=False, indent=2).encode("utf-8"),
            file_name="pubmed_results.json",
            mime="application/json",
        )

        # Optional: Save to PostgreSQL via API when configured
        if save_to_db:
            try:
                with st.spinner("Saving to PostgreSQL via API..."):
                    save_payload = {"records": records}
                    sresp = requests.post(f"{API_BASE}/save", json=save_payload, timeout=120)
                    if sresp.status_code != 200:
                        raise RuntimeError(f"API error {sresp.status_code}: {sresp.text}")
                    saved = sresp.json().get("saved", 0)
                st.success(f"Saved {saved} records to PostgreSQL.")
            except Exception as e:
                st.error(f"Database save failed: {e}")

    else:
        # Initial helper message before a search has been run
        st.info("Enter a query on the left and click Search.")

with tab_qa:
    st.subheader("Ask a question about your saved articles (title, year, journal)")
    col1, col2 = st.columns([3, 2])
    with col1:
        question = st.text_input("Question", placeholder="How many 2024 articles in Nature?")
    with col2:
        top_k = st.number_input("Max rows", min_value=1, max_value=1000, value=100, step=10)

    st.caption("The API uses an LLM to generate a safe SQL SELECT over the articles table. Keys/URLs are read from environment.")
    ask = st.button("Ask", type="primary")

    if ask:
        if not question:
            st.warning("Please enter a question.")
            st.stop()
        with st.spinner("Asking the API..."):
            try:
                payload = {
                    "question": question,
                    "top_k": int(top_k),
                }
                r = requests.post(f"{API_BASE}/qa", json=payload, timeout=120)
                if r.status_code != 200:
                    raise RuntimeError(f"API error {r.status_code}: {r.text}")
                resp = r.json()
            except Exception as e:
                st.error(f"Q&A request failed: {e}")
                st.stop()

        sql = resp.get("sql") or ""
        rows = resp.get("rows") or []
        st.markdown("Generated SQL:")
        st.code(sql, language="sql")
        if not rows:
            st.info("No rows returned.")
        else:
            import pandas as pd
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
