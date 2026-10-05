FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
RUN groupadd --system --gid 10001 sitelink && useradd --system --uid 10001 --gid 10001 sitelink && mkdir -p /data && chown -R sitelink:sitelink /app /data
ENV HOST=0.0.0.0
ENV PORT=8000
ENV DATA_DIR=/data
EXPOSE 8000
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
CMD ["python", "server.py"]
