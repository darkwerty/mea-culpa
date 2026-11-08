"""Configuración por variables de entorno. Sin Azure configurado, todo va en modo mock."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí"}


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    azure_endpoint: str | None
    azure_deployment: str | None
    azure_api_version: str | None
    azure_api_key: str | None
    azure_supports_temperature: bool
    llm_max_retries: int
    analysis_depth: int
    stockfish_depth: int
    data_dir: Path
    price_input_per_m: float
    price_output_per_m: float
    cors_origins: tuple[str, ...]


@lru_cache
def get_settings() -> Settings:
    return Settings(
        llm_provider=os.environ.get("LLM_PROVIDER", "mock").strip().lower(),
        azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT") or None,
        azure_deployment=os.environ.get("AZURE_OPENAI_DEPLOYMENT") or None,
        # Si se deja vacío se usa la API v1 (/openai/v1/), que no necesita api-version.
        azure_api_version=os.environ.get("AZURE_OPENAI_API_VERSION") or None,
        azure_api_key=os.environ.get("AZURE_OPENAI_API_KEY") or None,
        azure_supports_temperature=_env_bool("AZURE_OPENAI_SUPPORTS_TEMPERATURE", True),
        llm_max_retries=int(os.environ.get("LLM_MAX_RETRIES", "3")),
        analysis_depth=int(os.environ.get("ANALYSIS_DEPTH", "3")),
        stockfish_depth=int(os.environ.get("STOCKFISH_ANALYSIS_DEPTH", "14")),
        data_dir=Path(os.environ.get("MEA_CULPA_DATA_DIR", str(ROOT_DIR / "data" / "runtime"))),
        # Precios orientativos por millón de tokens (USD). Ajusta a tu modelo y región.
        price_input_per_m=float(os.environ.get("LLM_PRICE_INPUT_PER_M", "0.40")),
        price_output_per_m=float(os.environ.get("LLM_PRICE_OUTPUT_PER_M", "1.60")),
        cors_origins=tuple(
            o.strip()
            for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
            if o.strip()
        ),
    )
