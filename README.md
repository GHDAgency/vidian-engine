# Vidian Scan Engine

A small web service. POST a URL, it fetches the live site through Firecrawl,
runs 42 checkpoints across five sections, and returns scored JSON.
The dashboard calls this. The Firecrawl key lives here on the server, never in the page.

## Endpoints
- `GET  /`      health check
- `POST /scan`  body: `{"url":"https://example.com"}` -> scored JSON

## Deploy to Render (free tier)
1. Put these four files in a folder: app.py, requirements.txt, Procfile, render.yaml
2. Push the folder to a new GitHub repo (or upload it).
3. On render.com: New > Web Service > connect the repo.
4. Runtime: Python.  Build: `pip install -r requirements.txt`.  Start: `gunicorn app:app`.
5. Add an Environment Variable:  FIRECRAWL_API_KEY = your fc-... key.
6. Create. Render gives you a public URL like https://vidian-scan-engine.onrender.com
7. Test it:  the dashboard will POST to  <that URL>/scan

## Run locally
    pip install -r requirements.txt
    export FIRECRAWL_API_KEY=fc-xxxxx
    python3 app.py https://example.com        # one-off scan in the terminal
    gunicorn app:app                          # run the service on :8000
