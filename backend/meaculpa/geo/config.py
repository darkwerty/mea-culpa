"""Ajustes propios del juego (variables GEO_*). El resto (proveedores, login, almacén) es el de Mea culpa."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
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
    # Street View (Google Maps Platform). Sin clave del servidor, todas las partidas son con fotos de Commons.
    sv_key: str | None = field(default=None, repr=False)  # solo Street View Static API: nunca sale del servidor
    sv_images: int = 4  # capturas de Street View Static que recibe la IA por ronda
    sv_fov: int = 90  # campo de visión de cada captura (grados): 4 de 90 cubren la vuelta entera
    sv_size: str = "640x640"  # máximo sin plan premium
    sv_radius_m: int = 50_000  # radio de búsqueda de los metadatos alrededor de cada punto al azar
    sv_pool_target: int = 40  # pano_id de reserva para que crear una partida sea instantáneo
    sv_pool_low: int = 20  # por debajo de esto se rellena en segundo plano
    sv_pool_background: bool = True  # False en tests: el relleno se hace en la misma llamada
    quota_sv_games_per_day: int = 3  # partidas con Street View por persona y día (UTC); /admin la cambia
    # Topes propios por debajo de los de Google (que son 300 al día y se reinician a medianoche de California):
    sv_static_per_day: int = 280  # capturas de Street View Static al día; nunca más de SV_STATIC_HARD_CAP
    # Capturas que no se reservan para partidas nuevas: quedan para reintentar una ronda cuando Google falla.
    sv_static_slack: int = 20
    # Cargas de la Maps JavaScript API al día que cuentan en la cuota «Map loads» de Google: cada mapa y cada
    # panorámica de Street View (comprobado en Cloud Monitoring: cada `new StreetViewPanorama` es un loadMap). La
    # cuota en Google es de 160 al día (160 x 31 = 4.960, por debajo de las 5.000 panorámicas gratis al mes).
    # Las partidas nuevas paran en `maps_loads_per_day`; las recargas pueden llegar hasta MAPS_HARD_CAP.
    maps_loads_per_day: int = 140
    # Panorámicas de Street View dinámico al día (su SKU: 5.000 gratis al mes, unas 161 al día). Cada partida con
    # Street View crea una y cada recarga de la página, otra. Doble control por si Google dejara de contarlas arriba.
    sv_panoramas_per_day: int = 150
    quota_reloads_per_day: int = 10  # recargas de una partida a medias por persona y día (cada una, mapa y panorámica)
    # Para tests: sustituye a la petición HTTP a Google (url, params) -> (status, cuerpo, content-type).
    http_get: Callable[[str, dict], tuple[int, bytes, str]] | None = field(default=None, repr=False, compare=False)

    @property
    def sv_games_per_day(self) -> int:
        """Partidas con Street View al día (todas las personas) que caben en el tope de capturas."""
        usable = min(self.sv_static_per_day, SV_STATIC_HARD_CAP) - max(0, self.sv_static_slack)
        return max(0, usable // max(1, self.rounds * self.sv_images))


# Techo duro de capturas de Street View Static al día, por debajo de la cuota de 300 que hay puesta en Google.
SV_STATIC_HARD_CAP = 290
# Techo duro de panorámicas dinámicas al día: 155 x 31 = 4.805, por debajo de las 5.000 gratis al mes.
SV_PANORAMAS_HARD_CAP = 155
# Techo duro de cargas de mapa y panorámica al día, por debajo de la cuota de 160 «Map loads» puesta en Google.
MAPS_HARD_CAP = 155


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
        sv_key=(os.environ.get("GEO_GOOGLE_MAPS_KEY") or "").strip() or None,
        sv_images=max(1, min(6, _int("GEO_SV_IMAGES", 4))),
        sv_fov=max(30, min(120, _int("GEO_SV_FOV", 90))),
        sv_radius_m=_int("GEO_SV_RADIUS_M", 50_000),
        sv_pool_target=_int("GEO_SV_POOL_TARGET", 40),
        sv_pool_low=_int("GEO_SV_POOL_LOW", 20),
        quota_sv_games_per_day=_int("GEO_QUOTA_SV_GAMES_PER_DAY", 3),
        sv_static_per_day=min(SV_STATIC_HARD_CAP, _int("GEO_SV_STATIC_PER_DAY", 280)),
        maps_loads_per_day=min(MAPS_HARD_CAP, _int("GEO_MAPS_LOADS_PER_DAY", 140)),
        sv_static_slack=_int("GEO_SV_STATIC_SLACK", 20),
        sv_panoramas_per_day=min(SV_PANORAMAS_HARD_CAP, _int("GEO_SV_PANORAMAS_PER_DAY", 150)),
        quota_reloads_per_day=_int("GEO_QUOTA_RELOADS_PER_DAY", 10),
    )
