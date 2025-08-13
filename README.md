# SubMind OneClick (Max)

Single service: API + worker + UI + streaming + SQLite persistence.

## One-click on Render (Blueprint)
1) Create repo, upload all files in this folder.
2) Render → New → Blueprint → use this repo URL.
3) It will auto-create a Web Service with proper build/start.
4) Open the site.

## Manual
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port 10000`

## Env (optional)
- NEWSAPI_KEY: to enable NewsAPI headlines fetch (optional)
- WORKER_INTERVAL: seconds between fetch loops (default 20)
