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

## Notes
- This project is self-contained: core downloader modules live under `src/ranobelib_downloader/`, so no external `PYTHONPATH` is required.
- Long-running chapter generation runs on the server in a thread pool; UI polls `/api/tasks/{task_id}` for progress.
