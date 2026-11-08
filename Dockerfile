# Imagen de la API para Azure Container Apps. Se construye en la nube con `az acr build` (ACR privado del grupo).
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/darkwerty/mea-culpa" \
      org.opencontainers.image.description="API de Mea culpa (FastAPI)"

# MEA_CULPA_DATA_DIR: única ruta escribible por appuser (métricas del rival LLM; las partidas van a Blob).
# STOCKFISH_PATH: el paquete de Debian deja el binario en /usr/games (fuera del PATH por defecto).
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEA_CULPA_DATA_DIR=/tmp/mea-culpa \
    STOCKFISH_PATH=/usr/games/stockfish
WORKDIR /app

# Stockfish para el análisis "mea culpa" y para el rival Stockfish por nivel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/* \
    && test -x /usr/games/stockfish

# Versiones fijadas con constraints.txt: lo que se despliega es lo que pasó los tests.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt azure-monitor-opentelemetry -c constraints.txt

# Solo el paquete de la API: backend/dev_server.py (usuario local sin login) NO entra en la imagen.
COPY backend/meaculpa ./backend/meaculpa
COPY data/pgn ./data/pgn
# Fotos de ¿Dónde está esta foto? (Wikimedia Commons, 1280 px, sin EXIF) y su manifest con solución y atribución.
COPY data/fotos ./data/fotos

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "meaculpa.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
