# RanobeLIB Web

Single-page web client + lightweight FastAPI proxy for RanobeLIB downloader logic.

## Run locally
```bash
cd E:\ranobelib-web
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn src.web_app:app --host 127.0.0.1 --port 8080
```

Then open `http://localhost:8080` and paste a RanobeLIB novel URL.

## Project
- `src/web_app.py` — FastAPI proxy + static SPA serving
- `src/ranobelib_downloader/` — bundled core modules
- `web/index.html` — self-contained SPA UI
- `requirements.txt` — Python deps
- `Dockerfile` / `render.yaml` — free-tier deploy artifacts

## Deploy (Render, free tier)

1. Push this repo to GitHub (public or private).
2. In Render: **New → Blueprint** → connect the repo → Render reads `render.yaml`.
3. The blueprint creates a free web service (`docker` runtime) on port 8080 with:
   - `healthCheckPath: /api/health`
   - persistent disk `downloads` (1 GB) mounted at `/app/downloads`
   - `KEEPALIVE=1` (in-process ping every 10 min to avoid free-tier sleep)
4. First deploy builds the Docker image; subsequent requests may take ~30s if the service was idle.

Env vars (set in `render.yaml`, override in Render dashboard if needed):
- `KEEPALIVE` — `1` to enable the keep-alive thread (default on)
- `KEEPALIVE_INTERVAL` — seconds between pings (default `600`)
- `WEB_TOKEN` — optional: protect the instance (not enforced by default)

## Notes
- This project is self-contained: core downloader modules live under `src/ranobelib_downloader/`, so no external `PYTHONPATH` is required.
- Long-running chapter generation runs on the server in a thread pool; UI polls `/api/tasks/{task_id}` for progress.
- Memory is bounded: chapter cache is LRU (max 3 novels) and temp images are purged after 1h.
