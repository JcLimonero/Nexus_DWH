# Backend Nexus DWH (FastAPI, PostgreSQL) — imagen para Coolify/Docker (DWH_README.md §23).
# Contexto de construcción: dwh_back/ (Coolify: base directory /dwh_back, build pack Dockerfile).
# La configuración y los secretos llegan SOLO por variables de entorno (NEXUS__<SECCION>__<CLAVE>,
# NEXUS_CONFIG_SECRET_KEY, ...): no se copia ningún config.ini a la imagen (.dockerignore).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias de ejecución únicamente (sin requirements_dev.txt).
COPY requirements_postgres.txt ./
RUN pip install -r requirements_postgres.txt

# Usuario sin privilegios. El código queda como root (solo lectura para la app).
RUN groupadd --system --gid 10001 nexus \
    && useradd --system --uid 10001 --gid nexus --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin nexus

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh
COPY . .

USER nexus

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

# Proxy headers de uvicorn: apagados por defecto (§20.3). Detrás de Traefik se activan con
# NEXUS__SERVER__PROXY_HEADERS / NEXUS__SERVER__FORWARDED_ALLOW_IPS (ver §23).
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "main_postgres.py", "--host", "0.0.0.0", "--port", "8000"]
