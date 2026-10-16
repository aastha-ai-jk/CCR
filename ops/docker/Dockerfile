# CCR Kiln-3 twin — Python image shared by every backend-side service
# (API + advisor page, live OPC feed, shadow, optimizer loop, scheduler for
# monitor/retrain, optional eLIMS feed). Data and models are volumes.
#   docker compose build      (from the repo root)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata

# libgomp1: LightGBM's OpenMP runtime; tzdata: plant-local timestamps
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-ml.txt ./
# opcua: the live feed's OPC UA client (KEPServer), not listed in requirements.txt
RUN pip install -r requirements.txt -r requirements-ml.txt opcua

COPY config.py ./
COPY backend ./backend
COPY etl ./etl
COPY twin ./twin
COPY ops/docker/scheduler.py ./ops/docker/scheduler.py

# Run as a non-root user whose UID matches the owner of the host's data/ and
# models/ folders (on the GPU box: `id -u antpc`), so the volumes stay writable.
ARG APP_UID=1000
RUN useradd --uid "${APP_UID}" --create-home ccr \
 && mkdir -p /app/data /app/models \
 && chown -R ccr:ccr /app
USER ccr

EXPOSE 8010
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8010"]
