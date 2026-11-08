"""Ajustes propios del juego (variables GEO_*). El resto (proveedores, login, almacén) es el de Mea culpa."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import ROOT_DIR
from ..models import MODEL_CATALOG

# Modelos del catálogo (models.MODEL_CATALOG) que aceptan imágenes: los marcados `vision`, comprobados con una
# llamada real con una foto del juego (los que devuelven 400 con imagen o respuestas sin sentido no lo llevan).
VISION_MODEL_IDS = tuple(m.id for m in MODEL_CATALOG if m.vision)
# Descripciones propias del juego cuando la del catálogo habla de ajedrez.
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
