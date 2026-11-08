"""Gasto de Azure del mes en curso (Cost Management Query API) para el panel /admin.

La identidad administrada de la API tiene el rol «Cost Management Reader» sobre la suscripción. El resultado se
cachea unos minutos: la API de costes tiene límites de peticiones bajos y los datos se actualizan pocas veces al día.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("meaculpa.costes")

MANAGEMENT_SCOPE = "https://management.azure.com/.default"
API_VERSION = "2023-11-01"
QUERY = {
    "type": "ActualCost",
    "timeframe": "MonthToDate",
    "dataset": {
        "granularity": "None",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        "grouping": [{"type": "Dimension", "name": "ServiceName"}],
    },
}

Fetcher = Callable[[], dict[str, Any]]  # devuelve el JSON de la Query API


def parse_query(body: dict[str, Any]) -> tuple[float, str, list[dict[str, Any]]]:
    props = body.get("properties") or {}
    names = [str(c.get("name", "")).lower() for c in props.get("columns") or []]
    i_cost = next((i for i, n in enumerate(names) if n in {"cost", "pretaxcost", "costusd"}), 0)
    i_service = names.index("servicename") if "servicename" in names else None
    i_currency = names.index("currency") if "currency" in names else None
    services, total, currency = [], 0.0, "EUR"
    for row in props.get("rows") or []:
        cost = float(row[i_cost] or 0)
        total += cost
        if i_currency is not None and row[i_currency]:
            currency = str(row[i_currency])
        services.append({"servicio": str(row[i_service]) if i_service is not None else "?", "coste": round(cost, 4)})
    services.sort(key=lambda s: s["coste"], reverse=True)
    return round(total, 4), currency, services


def http_fetcher(subscription_id: str, credential: Any) -> Fetcher:
    def fetch() -> dict[str, Any]:
        import requests

        token = credential.get_token(MANAGEMENT_SCOPE).token
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.CostManagement/query?api-version={API_VERSION}"
        )
        res = requests.post(url, json=QUERY, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        res.raise_for_status()
        return res.json()

    return fetch


class AzureCosts:
    TTL = 600.0  # 10 minutos

    def __init__(self, fetch: Fetcher | None, budget_eur: float) -> None:
        self.fetch = fetch
        self.budget = budget_eur
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._at = -1e9

    def month(self) -> dict[str, Any]:
        base = {"presupuesto": self.budget, "moneda": "EUR"}
        if self.fetch is None:
            return {**base, "disponible": False, "error": "Falta AZURE_SUBSCRIPTION_ID en la configuración"}
        with self._lock:
            if self._cached and time.monotonic() - self._at < self.TTL:
                return self._cached
            try:
                total, currency, services = parse_query(self.fetch())
                self._cached = {
                    **base,
                    "disponible": True,
                    "total": total,
                    "moneda": currency,
                    "por_servicio": services,
                    "porcentaje": round(100 * total / self.budget, 1) if self.budget else None,
                    "consultado": time.time(),
                }
                self._at = time.monotonic()
                return self._cached
            except Exception as exc:  # noqa: BLE001 - sin permisos, límite de peticiones o sin red
                log.warning("No se pudo consultar Cost Management: %s", exc)
                if self._cached:
                    return {**self._cached, "aviso": "Dato de la última consulta correcta"}
                return {**base, "disponible": False, "error": "No se pudo consultar Cost Management ahora mismo"}
