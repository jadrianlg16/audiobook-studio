# Build context is the PROJECT ROOT (see docker-compose.yml) so we can copy the
# shared contract package alongside the gateway.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY gateway/requirements.txt /app/gateway/requirements.txt
RUN pip install --no-cache-dir -r /app/gateway/requirements.txt

COPY shared /app/shared
COPY gateway /app/gateway

EXPOSE 8000
CMD ["uvicorn", "gateway.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
