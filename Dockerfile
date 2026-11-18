
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# Install Bash and PostgreSQL client for automatic provisioning.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash \
       postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged application user and persistent data directory.
RUN groupadd --gid 10001 dir \
    && useradd --uid 10001 --gid 10001 \
       --create-home --shell /usr/sbin/nologin dir \
    && mkdir -p /data \
    && chown -R dir:dir /data

# Copy PostgreSQL provisioning script into the image.
COPY --chown=dir:dir docker/provision/ /app/docker/provision/

# Make provisioning script executable.
RUN chmod 755 /app/docker/provision/01-create-monitoring-role.sh

# Install Python dependencies separately for better build caching.
COPY requirements.txt .

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

# Copy application and frontend.
COPY --chown=dir:dir app/ ./app/
COPY --chown=dir:dir static/ ./static/

# Run application without root privileges.
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

