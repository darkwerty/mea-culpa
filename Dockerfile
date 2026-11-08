# Imagen de la API (para Azure Container Apps). Escrita, no construida ni publicada todavía.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt azure-monitor-opentelemetry

COPY backend/meaculpa ./backend/meaculpa
COPY data/pgn ./data/pgn

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "meaculpa.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
