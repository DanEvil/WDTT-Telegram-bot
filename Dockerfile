FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN addgroup --system app \
    && adduser --system --ingroup app appuser \
    && mkdir -p /app/data \
    && chown -R appuser:app /app

USER appuser
ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/bot.db

CMD ["python", "bot.py"]
