"""«¿Dónde está esta foto?»: juego de geolocalización contra un modelo de IA con visión.

Subpaquete de la API de Mea culpa: usa su login (auth.User), su almacén (storage.ObjectStore), sus cuotas
(quota.UsageLimiter) y sus proveedores de LLM (models.MODEL_CATALOG, llm.*). Se monta con
`create_geo_router(...)` bajo /api/geo.
"""

from .router import GEO_PREFIX, create_geo_router

__all__ = ["GEO_PREFIX", "create_geo_router"]
