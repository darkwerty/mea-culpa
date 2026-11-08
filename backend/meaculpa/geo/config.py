"""Ajustes propios del juego (variables GEO_*). El resto (proveedores, login, almacén) es el de Mea culpa."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import ROOT_DIR

# Modelos del catálogo de Mea culpa (models.MODEL_CATALOG) que aceptan imágenes, según la documentación:
# - gpt-4.1-mini, gpt-6-luna, gpt-6.1-terra: Microsoft Learn, «Foundry Models sold by Azure»
#   (act. 2026-10-29): «Text and image input with text output» / «Text and image processing».
#   gpt-6-luna-high es el mismo deployment gpt-6-luna con razonamiento alto.
# - claude-haiku-5-5: catálogo de Foundry (ai.azure.com/catalog/models/claude-haiku-5-5): «text and image input».
VISION_MODEL_IDS = ("gpt-6-luna", "gpt-6-luna-high", "gpt-6.1-terra", "claude-haiku-5-5", "gpt-4.1-mini")
# Descripciones propias del juego cuando la del catálogo de Mea culpa habla de ajedrez.
GEO_DESCRIPTIONS = {"gpt-6-luna-high": "Razonamiento alto, piensa cada foto"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.strip() else default


@dataclass(frozen=True)
class GeoSettings:
    photos_dir: Path
    rounds: int = 5
    quota_games_per_day: int = 20  # partidas nuevas por usuario y día (UTC)
    quota_llm_per_day: int = 100  # fotos analizadas por la IA por usuario y día
    global_llm_per_day: int = 1000  # fusible del juego, además del global de toda la demo
    image_detail: str = "high"  # detail de la imagen en Azure OpenAI: low | high | auto
    models: tuple[str, ...] = ()  # vacío = todos los modelos con visión activos en Mea culpa
    default_model: str | None = None


def get_geo_settings() -> GeoSettings:
    return GeoSettings(
        photos_dir=Path(os.environ.get("GEO_PHOTOS_DIR") or ROOT_DIR / "data" / "fotos"),
        rounds=_int("GEO_ROUNDS", 5),
        quota_games_per_day=_int("GEO_QUOTA_GAMES_PER_DAY", 20),
        quota_llm_per_day=_int("GEO_QUOTA_LLM_PER_DAY", 100),
        global_llm_per_day=_int("GEO_GLOBAL_LLM_PER_DAY", 1000),
        image_detail=(os.environ.get("GEO_IMAGE_DETAIL") or "high").strip().lower(),
        models=tuple(m.strip() for m in os.environ.get("GEO_MODELS", "").split(",") if m.strip()),
        default_model=(os.environ.get("GEO_DEFAULT_MODEL") or "").strip() or None,
    )
