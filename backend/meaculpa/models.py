"""Catálogo de modelos de Foundry que se pueden elegir como rival IA.

La API publica el catálogo completo (`GET /api/meaculpa/models`) con `disponible` por modelo: solo se pueden
usar los que estén activados en `LLM_MODELS` (lista de ids separados por comas) y tengan su deployment creado
en Foundry. Así el selector del frontend muestra también los que faltan, sin fingir que funcionan.

Las opciones de cada entrada son las del experimento de Elo (eval/experimentos/pago.yaml).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .llm.base import LLMProvider

log = logging.getLogger("meaculpa")


@dataclass(frozen=True)
class ModelSpec:
    id: str
    etiqueta: str
    descripcion: str
    proveedor: str  # "azure" (Azure OpenAI en Foundry) | "claude" (Messages API en Foundry)
    deployment: str
    opciones: dict[str, Any] = field(default_factory=dict)


MODEL_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        "gpt-4.1-mini",
        "GPT-4.1 mini",
        "Rápido y barato, sin razonamiento",
        "azure",
        "gpt-4.1-mini",
        {"supports_temperature": True, "price_input_per_m": 0.40, "price_output_per_m": 1.60},
    ),
    ModelSpec(
        "gpt-6-luna",
        "GPT-6 Luna",
        "Sin razonamiento",
        "azure",
        "gpt-6-luna",
        {
            "reasoning_effort": "none",
            "max_tokens": 400,
            "supports_temperature": False,
            "price_input_per_m": 0.10,
            "price_output_per_m": 0.50,
        },
    ),
    ModelSpec(
        "gpt-6-luna-high",
        "GPT-6 Luna",
        "Razonamiento alto, piensa cada jugada",
        "azure",
        "gpt-6-luna",
        {
            "reasoning_effort": "high",
            "max_tokens": 16000,
            "supports_temperature": False,
            "price_input_per_m": 0.10,
            "price_output_per_m": 0.50,
        },
    ),
    ModelSpec(
        "gpt-6.1-terra",
        "GPT-6.1 Terra",
        "El más fuerte de la familia GPT en Foundry",
        "azure",
        "gpt-6.1-terra",
        {"max_tokens": 16000, "supports_temperature": False, "price_input_per_m": 1.00, "price_output_per_m": 5.00},
    ),
    ModelSpec(
        "claude-haiku-5-5",
        "Claude Haiku 5.5",
        "Anthropic en Foundry, esfuerzo bajo",
        "claude",
        "claude-haiku-5-5",
        {"effort": "low", "max_tokens": 4000, "price_input_per_m": 0.10, "price_output_per_m": 0.50},
    ),
)

CATALOG_BY_ID = {m.id: m for m in MODEL_CATALOG}


def _build(spec: ModelSpec, settings: Settings) -> LLMProvider:
    opts = dict(spec.opciones)
    if spec.proveedor == "azure":
        from .llm.azure import AzureOpenAIProvider

        if not settings.azure_endpoint:
            raise RuntimeError("falta AZURE_OPENAI_ENDPOINT")
        return AzureOpenAIProvider(
            settings.azure_endpoint,
            spec.deployment,
            api_version=settings.azure_api_version,
            api_key=settings.azure_api_key,
            auth=settings.azure_auth,
            name=spec.id,
            structured_output="on",
            **opts,
        )
    from .llm.claude import ClaudeFoundryProvider

    resource = os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or None
    base_url = os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL") or None
    if not resource and not base_url:
        raise RuntimeError("falta ANTHROPIC_FOUNDRY_RESOURCE")
    return ClaudeFoundryProvider(
        spec.deployment,
        resource=resource,
        base_url=base_url,
        api_key=os.environ.get("ANTHROPIC_FOUNDRY_API_KEY") or None,
        auth=settings.azure_auth,
        name=spec.id,
        **opts,
    )


def build_model_providers(settings: Settings) -> dict[str, LLMProvider]:
    """Proveedores de los modelos activados en `settings.llm_models` (los que fallen al crearse se omiten)."""
    out: dict[str, LLMProvider] = {}
    for model_id in settings.llm_models:
        spec = CATALOG_BY_ID.get(model_id)
        if spec is None:
            log.warning("LLM_MODELS: %s no está en el catálogo", model_id)
            continue
        try:
            out[model_id] = _build(spec, settings)
        except Exception as exc:  # noqa: BLE001 - un modelo mal configurado no tumba la API
            log.warning("No se pudo crear el modelo %s: %s", model_id, exc)
    return out


def model_catalog(models: dict[str, LLMProvider], default: str | None) -> list[dict[str, Any]]:
    """Catálogo para el frontend. Los modelos activos que no estén en el catálogo (el mock) van al final."""
    rows = [
        {
            "id": m.id,
            "etiqueta": m.etiqueta,
            "descripcion": m.descripcion,
            "proveedor": "Anthropic" if m.proveedor == "claude" else "OpenAI",
            "disponible": m.id in models,
            "por_defecto": m.id == default,
        }
        for m in MODEL_CATALOG
    ]
    for model_id, provider in models.items():
        if model_id not in CATALOG_BY_ID:
            rows.append(
                {
                    "id": model_id,
                    "etiqueta": provider.name,
                    "descripcion": "Modelo local de pruebas",
                    "proveedor": provider.kind,
                    "disponible": True,
                    "por_defecto": model_id == default,
                }
            )
    return rows
