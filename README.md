# PubMed

A simple PubMed search UI built with Streamlit.

## Quick start

1) Create a virtual environment (optional but recommended), then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Run the app:

```bash
streamlit run src/app.py
```

3) In the UI, enter your search query, optional date range, and choose whether to include abstracts. You can also provide an NCBI API key to raise rate limits.

Exports are available as CSV and JSON.

## Notes

- Without an API key, NCBI rate limits are lower (about 3 req/s). With an API key, up to ~10 req/s. The app adds small delays to respect limits.
- Abstracts require an extra fetch (XML parse); if you only need metadata, uncheck "Include abstracts" for faster results.
- Fields returned: PMID, Title, Authors, Journal, PubDate, DOI, Abstract (if included).

## PostgreSQL 

This app can persist results to PostgreSQL using SQLAlchemy.

1) Install dependencies (already in requirements.txt): SQLAlchemy, psycopg[binary], python-dotenv

2) First run will auto-create the `articles` table. Then enable "Save results to PostgreSQL" in the UI and run a search. You'll see a success message with the number of saved records.

Schema (simplified):
- articles(id PK, pmid UNIQUE, title, authors(TEXT[]), journal, pubdate, doi, abstract, created_at, updated_at)

Troubleshooting:
- If you see "DATABASE_URL is not set", ensure the URL is provided either via the UI or environment.
- For connection errors, verify host/port, credentials, and that the DB accepts TCP connections.

## FastAPI backend

Run the server:

```bash
uvicorn src.api:app --reload --port 8000
```

Endpoints:
- GET http://127.0.0.1:8000/health → {"status":"ok"}
- POST http://127.0.0.1:8000/search
	- Body JSON: {"query":"...","retmax":50,"mindate":"YYYY/MM/DD","maxdate":"YYYY/MM/DD","include_abstracts":true,"api_key":"..."}
- POST http://127.0.0.1:8000/save
	- Body JSON: {"database_url":"postgresql+psycopg://...","records":[{...}]}

This uses the same `pubmed_client` and `db` modules as the Streamlit app.

### Q&A (Natural language → SQL)

The app includes a Q&A tab that lets you ask questions about your saved articles using natural language. Under the hood, the FastAPI endpoint `/qa` uses an LLM to generate a safe `SELECT` query against the `articles` table and returns the results.

Run the API:

```bash
uvicorn src.api:app --reload --port 8000
```

In Streamlit, open the Q&A tab, provide your `DATABASE_URL`, type a question (e.g., "How many 2024 articles in Nature?"), and click Ask. The UI shows the generated SQL and the result rows.
