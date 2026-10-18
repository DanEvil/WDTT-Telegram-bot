# ---------- Builder ----------
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Runtime ----------
FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY . .
RUN adduser --system --group appuser && chown -R appuser /app
USER appuser
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]