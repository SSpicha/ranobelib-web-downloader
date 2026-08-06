FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev libjpeg62-turbo-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Self-contained repo: core lives at src/ranobelib_downloader/ (no external checkout needed).
COPY src /app/src
COPY web /app/web
CMD ["uvicorn", "src.web_app:app", "--host", "0.0.0.0", "--port", "8080"]
