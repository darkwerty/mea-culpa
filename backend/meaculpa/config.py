"""Configuración por variables de entorno. Sin Azure configurado, todo va en modo mock."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ADMIN_EMAILS = ("admin@example.com",)


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
    quota_hints_per_day: int = 30
    global_llm_calls_per_day: int = 3000
    # Fusible de gasto en IA de las dos demos, en céntimos de dólar al día (50 = 0,50 USD, unos 15 al mes).
    global_llm_cents_per_day: int = 50
    # Y por persona y día (20 = 0,20 USD): una partida con un modelo caro no agota el de todos.
    user_llm_cents_per_day: int = 20
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
    # Azure SQL (oferta gratuita): solo servidor y base de datos; se entra con la identidad administrada.
    # Vacío = SQLite en data_dir (local y tests).
    sql_server: str | None = None
    sql_database: str | None = None
    # Panel /admin: correos de Google (verificados) con acceso. Cualquier otro recibe 403.
    admin_emails: tuple[str, ...] = DEFAULT_ADMIN_EMAILS
    # Gasto de Azure en el panel: suscripción para Cost Management y límite mensual de referencia.
    azure_subscription_id: str | None = None
    azure_budget_eur: float = 40.0
    # Formulario de contacto: envíos por IP y día, y total de envíos al día.
    contact_per_ip_per_day: int = 5
    contact_per_day: int = 100
    # Barra de evaluación en vivo (POST /eval): solo Stockfish, tiempo y profundidad cortos, sin base de datos.
    eval_time_ms: int = 200
    eval_depth: int = 14
    eval_interval_ms: int = 150  # media mínima entre peticiones de una persona (con algo de ráfaga)
    eval_per_day: int = 5000  # tope diario por persona (en memoria de cada réplica)
    # Pista (POST /games/{id}/hint): algo más de motor que la barra.
    hint_time_ms: int = 600
    hint_depth: int = 18
    # Procesos de Stockfish reutilizables para la barra y las pistas (el análisis de partidas abre el suyo).
    engine_pool_size: int = 2
    engine_timeout_s: float = 5.0


def parse_admin_emails(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_ADMIN_EMAILS
    return tuple(e.strip().lower() for e in raw.split(",") if e.strip())


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
        quota_hints_per_day=_env_int("QUOTA_HINTS_PER_DAY", 30),
        global_llm_calls_per_day=_env_int("GLOBAL_LLM_CALLS_PER_DAY", 3000),
        global_llm_cents_per_day=_env_int("GLOBAL_LLM_CENTS_PER_DAY", 50),
        user_llm_cents_per_day=_env_int("USER_LLM_CENTS_PER_DAY", 20),
        max_analysis_plies=_env_int("MAX_ANALYSIS_PLIES", 200),
        max_concurrent_analyses=_env_int("MAX_CONCURRENT_ANALYSES", 1),
        analysis_timeout_s=float(os.environ.get("ANALYSIS_TIMEOUT_S", "90")),
        azure_auth=(os.environ.get("AZURE_AUTH") or "default").strip().lower(),
        azure_reasoning_effort=(os.environ.get("AZURE_OPENAI_REASONING_EFFORT") or "").strip() or None,
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 0) or None,
        llm_models=tuple(m.strip() for m in os.environ.get("LLM_MODELS", "").split(",") if m.strip()),
        llm_default_model=(os.environ.get("LLM_DEFAULT_MODEL") or "").strip() or None,
        sql_server=(os.environ.get("AZURE_SQL_SERVER") or "").strip() or None,
        sql_database=(os.environ.get("AZURE_SQL_DATABASE") or "").strip() or None,
        admin_emails=parse_admin_emails(os.environ.get("ADMIN_EMAILS")),
        azure_subscription_id=(os.environ.get("AZURE_SUBSCRIPTION_ID") or "").strip() or None,
        azure_budget_eur=float(os.environ.get("AZURE_BUDGET_EUR") or "40"),
        contact_per_ip_per_day=_env_int("CONTACT_PER_IP_PER_DAY", 5),
        contact_per_day=_env_int("CONTACT_PER_DAY", 100),
        eval_time_ms=_env_int("EVAL_TIME_MS", 200),
        eval_depth=_env_int("EVAL_DEPTH", 14),
        eval_interval_ms=_env_int("EVAL_INTERVAL_MS", 150),
        eval_per_day=_env_int("EVAL_PER_DAY", 5000),
        hint_time_ms=_env_int("HINT_TIME_MS", 600),
        hint_depth=_env_int("HINT_DEPTH", 18),
        engine_pool_size=_env_int("ENGINE_POOL_SIZE", 2),
        engine_timeout_s=float(os.environ.get("ENGINE_TIMEOUT_S") or "5"),
    )
