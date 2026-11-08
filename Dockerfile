# Imagen de la API para Azure Container Apps. La construye y publica en GHCR .github/workflows/deploy.yml.
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/darkwerty/mea-culpa" \
      org.opencontainers.image.description="API de Mea culpa (FastAPI)"

# MEA_CULPA_DATA_DIR: única ruta escribible por appuser (métricas del rival LLM; las partidas van a Blob).
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEA_CULPA_DATA_DIR=/tmp/mea-culpa
WORKDIR /app

# Versiones fijadas con constraints.txt: lo que se despliega es lo que pasó los tests.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt azure-monitor-opentelemetry -c constraints.txt

# Solo el paquete de la API: backend/dev_server.py (usuario local sin login) NO entra en la imagen.
COPY backend/meaculpa ./backend/meaculpa
COPY data/pgn ./data/pgn

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "meaculpa.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
