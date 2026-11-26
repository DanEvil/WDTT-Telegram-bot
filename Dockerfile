# ============================================================
# DOCKERFILE FOR TELEGRAM BOT
# ============================================================
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source code
COPY bot/ /app/

# Create directories
RUN mkdir -p /app/data /app/logs /app/config

# Create non-root user
RUN adduser --disabled-password --gecos "" appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "-m", "bot"]