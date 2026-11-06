FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN addgroup --system app \
    && adduser --system --ingroup app appuser \
    && mkdir -p /app/data /app/data/backups \
    && chown -R appuser:app /app

USER appuser
ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/bot.db

# Healthcheck: бот слушает /health на HEALTH_PORT (по умолчанию 8081)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3); sys.exit(0)" || exit 1

CMD ["python", "bot.py"]
