FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    RELAY_DB=/data/relay.db RELAY_TRUST_PROXY=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY relay ./relay
COPY agent_relay_worker ./agent_relay_worker
RUN pip install --no-cache-dir . && useradd -r -u 10001 relay && mkdir -p /data && chown relay /data
USER relay
EXPOSE 8080
CMD ["python", "-m", "relay", "--host", "0.0.0.0", "--port", "8080"]
