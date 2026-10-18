FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

CMD ["python", "bot.py"]