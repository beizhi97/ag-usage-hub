FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AG_DB_PATH=/app/data/usage.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY SPEC.md README.md ./

RUN mkdir -p /app/data

EXPOSE 8787

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers", "--forwarded-allow-ips", "*"]
