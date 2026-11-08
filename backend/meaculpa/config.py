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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.strip() else default


def parse_cors_origins(raw: str) -> tuple[str, ...]:
    """Orígenes CORS separados por comas. Nunca '*': la API recibe un token en la cabecera Authorization."""
    origins = tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip())
    for origin in origins:
        if "*" in origin:
            raise ValueError("CORS_ORIGINS no puede contener '*': indica los dominios exactos del frontend")
        if not origin.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError(f"Origen CORS no válido (solo https, o http en localhost): {origin}")
    return origins


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
    # Login con Google (ID token de Google Identity Services). Obligatorio: sin él la API no arranca.
    google_client_id: str | None = None
    # Persistencia en Azure Blob (identidad administrada). Vacío = ficheros en data_dir (solo en local).
    storage_account_url: str | None = None
    storage_container: str = "meaculpa"
    # Límites de uso (por usuario y día UTC) y fusible global de llamadas al LLM.
    quota_games_per_day: int = 30
    quota_moves_per_day: int = 1500
    quota_llm_moves_per_day: int = 200
    quota_analyses_per_day: int = 20
    global_llm_calls_per_day: int = 3000
    # Análisis: tope de plies, análisis simultáneos por réplica y tiempo máximo.
    max_analysis_plies: int = 200
    max_concurrent_analyses: int = 1
    analysis_timeout_s: float = 90.0
    # Autenticación con Entra ID: "default" (DefaultAzureCredential), "cli" (AzureCliCredential) o "key".
    azure_auth: str = "default"
    # Solo se envían si se configuran (None = valor por defecto del proveedor).
    azure_reasoning_effort: str | None = None
    llm_max_tokens: int | None = None
    # Modelos elegibles como rival IA (ids de models.MODEL_CATALOG) y el que se usa si no se elige ninguno.
    llm_models: tuple[str, ...] = ()
    llm_default_model: str | None = None


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
        llm_max_retries=_env_int("LLM_MAX_RETRIES", 3),
        analysis_depth=_env_int("ANALYSIS_DEPTH", 3),
        stockfish_depth=_env_int("STOCKFISH_ANALYSIS_DEPTH", 14),
        data_dir=Path(os.environ.get("MEA_CULPA_DATA_DIR", str(ROOT_DIR / "data" / "runtime"))),
        # Precios orientativos por millón de tokens (USD). Ajusta a tu modelo y región.
        price_input_per_m=float(os.environ.get("LLM_PRICE_INPUT_PER_M", "0.40")),
        price_output_per_m=float(os.environ.get("LLM_PRICE_OUTPUT_PER_M", "1.60")),
        cors_origins=parse_cors_origins(os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")),
        google_client_id=(os.environ.get("GOOGLE_CLIENT_ID") or "").strip() or None,
        storage_account_url=(os.environ.get("AZURE_STORAGE_ACCOUNT_URL") or "").strip() or None,
        storage_container=os.environ.get("AZURE_STORAGE_CONTAINER", "meaculpa").strip() or "meaculpa",
        quota_games_per_day=_env_int("QUOTA_GAMES_PER_DAY", 30),
        quota_moves_per_day=_env_int("QUOTA_MOVES_PER_DAY", 1500),
        quota_llm_moves_per_day=_env_int("QUOTA_LLM_MOVES_PER_DAY", 200),
        quota_analyses_per_day=_env_int("QUOTA_ANALYSES_PER_DAY", 20),
        global_llm_calls_per_day=_env_int("GLOBAL_LLM_CALLS_PER_DAY", 3000),
        max_analysis_plies=_env_int("MAX_ANALYSIS_PLIES", 200),
        max_concurrent_analyses=_env_int("MAX_CONCURRENT_ANALYSES", 1),
        analysis_timeout_s=float(os.environ.get("ANALYSIS_TIMEOUT_S", "90")),
        azure_auth=(os.environ.get("AZURE_AUTH") or "default").strip().lower(),
        azure_reasoning_effort=(os.environ.get("AZURE_OPENAI_REASONING_EFFORT") or "").strip() or None,
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 0) or None,
        llm_models=tuple(m.strip() for m in os.environ.get("LLM_MODELS", "").split(",") if m.strip()),
        llm_default_model=(os.environ.get("LLM_DEFAULT_MODEL") or "").strip() or None,
    )
