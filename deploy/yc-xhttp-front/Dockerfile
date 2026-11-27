FROM python:3.12-slim
RUN pip install --no-cache-dir aiohttp==3.10.*
WORKDIR /app
COPY proxy.py .
ENV LISTEN=0.0.0.0:8080
EXPOSE 8080
CMD ["python3", "proxy.py"]
