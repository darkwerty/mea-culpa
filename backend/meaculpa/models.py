"""Catálogo de modelos de Foundry que se pueden elegir como rival IA (ajedrez y juego de fotos).

La API publica el catálogo (`GET /api/meaculpa/models` y `GET /api/geo/models`) con `disponible` por modelo.
Se usan los modelos activados en `LLM_MODELS`: ids separados por comas, o `todos` (todos los no ocultos), y se
pueden añadir ocultos por id: `LLM_MODELS=todos,gpt-6-luna`.

Cada modelo lleva su vía de acceso (`proveedor`):
- "azure": Azure OpenAI en Foundry, API v1 (`/openai/v1/`), con salida estructurada (gpt-4.1-mini, gpt-5-mini).
- "foundry": endpoint de inferencia de modelos de Foundry (`/models`), para los modelos que no son de OpenAI
  (Meta, Mistral, xAI, DeepSeek, Moonshot, Microsoft, Cohere). Jugada en texto y JSON en el texto.
- "claude": Messages API de Anthropic en Foundry.
Algún modelo muy reciente solo responde por la API v1: va como "azure" con `structured_output="off"`.

`vision`: acepta imágenes y responde bien en el juego de fotos (comprobado con una llamada real con imagen).
`ajedrez`: responde jugadas legales en el formato pedido (comprobado con partidas de prueba).
`razona`: piensa antes de responder; tarda más y el frontend lo avisa.
`oculto`: sin cuota en la suscripción (cuota 0 en todas las regiones). No se publica salvo que se active a mano
en LLM_MODELS cuando haya cuota y deployment.

Precios en USD por millón de tokens (entrada, salida), de la API de precios de Azure (Global, noviembre de 2026);
los marcados «aprox.» no aparecen en ella y son una estimación. Solo sirven para el contador de coste y el
fusible diario.

Modelos probados y descartados (deployment creado, pero fuera del catálogo; 8 de noviembre de 2026):
- Phi-4-reasoning, Phi-4-mini-reasoning, Phi-4-mini-instruct y Phi-4-multimodal-instruct: más de 120 s por jugada
  (o sin jugada en el formato pedido) y, con imagen, 400 o respuestas sin sentido.
- Phi-4: 8 propuestas ilegales en 6 jugadas y 17 s por jugada. Ministral-3B: 4 de 6 jugadas de respaldo.
- DeepSeek-V3.2-Speciale: 36 s y 1,4 céntimos por jugada, agotando el tope de salida.
- Kimi-K2.6 solo en el juego de fotos (en ajedrez, más de un minuto por jugada).
- Kimi-K3.1-Pro, en el juego de fotos: agota 4000 tokens de razonamiento sin responder.
- o4-mini y gpt-4o-mini: Azure ya no deja crear deployments nuevos (modelos retirados o en retirada).
Sin imagen (400 con una foto): Llama 3.3, DeepSeek V3.2/V4/V4 Pro, gpt-oss, MAI-Thinking-1, Command A, Codestral,
Ministral. Esos solo juegan al ajedrez.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .llm.base import LLMProvider

log = logging.getLogger("meaculpa")

ALL_MODELS = "todos"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    etiqueta: str
    descripcion: str
    proveedor: str  # "azure" | "foundry" | "claude"
    deployment: str
    opciones: dict[str, Any] = field(default_factory=dict)
    fabricante: str = "OpenAI"
    vision: bool = False
    ajedrez: bool = True
    razona: bool = False
    oculto: bool = False
    # Variable de entorno con el endpoint de otra cuenta de Foundry (None = AZURE_OPENAI_ENDPOINT).
    endpoint_env: str | None = None


def _p(entrada: float, salida: float, **extra: Any) -> dict[str, Any]:
    return {"price_input_per_m": entrada, "price_output_per_m": salida, **extra}


# Tope de salida de los que razonan: acota el coste y el tiempo de cada llamada.
REASONING_MAX_TOKENS = 6000
TEXT_MAX_TOKENS = 1500

MODEL_CATALOG: tuple[ModelSpec, ...] = (
    # ---------------------------------------------------------------- OpenAI (Azure OpenAI, API v1)
    ModelSpec(
        "gpt-4.1-mini",
        "GPT-4.1 mini",
        "Rápido y barato, sin razonamiento",
        "azure",
        "gpt-4.1-mini",
        _p(0.40, 1.60, supports_temperature=True),
        vision=True,
    ),
    ModelSpec(
        "gpt-5-mini",
        "GPT-5 mini",
        "Razonamiento bajo",
        "azure",
        "gpt-5-mini",
        _p(0.25, 2.00, reasoning_effort="low", max_tokens=REASONING_MAX_TOKENS, supports_temperature=False),
        vision=True,
        razona=True,
    ),
    ModelSpec(
        "gpt-5.4-mini",
        "GPT-5.4 mini",
        "El más nuevo de OpenAI con cuota, sin razonamiento",
        "azure",
        "gpt-5.4-mini",  # DataZoneStandard: datos dentro de la UE
        _p(0.825, 4.95, reasoning_effort="none", max_tokens=REASONING_MAX_TOKENS, supports_temperature=False),
        vision=True,
    ),
    ModelSpec(
        "gpt-4.1",
        "GPT-4.1",
        "El GPT-4.1 completo, sin razonamiento",
        "azure",
        "gpt-4.1",  # Standard regional en Suecia Central (cuenta meaculpa-swe-7952)
        _p(2.42, 9.68, supports_temperature=True),
        vision=True,
        endpoint_env="AZURE_OPENAI_ENDPOINT_SWE",
    ),
    ModelSpec(
        "gpt-4o",
        "GPT-4o",
        "Multimodal clásico de OpenAI",
        "azure",
        "gpt-4o",  # Standard regional en Francia Central
        _p(3.025, 12.10, supports_temperature=True),
        vision=True,
    ),
    ModelSpec(
        "gpt-oss-120b",
        "gpt-oss-120b",
        "Modelo abierto de OpenAI, razona",
        "foundry",
        "gpt-oss-120b",
        _p(0.15, 0.60, max_tokens=REASONING_MAX_TOKENS),
        razona=True,
    ),
    # ---------------------------------------------------------------- Meta
    ModelSpec(
        "llama-4-maverick",
        "Llama 4 Maverick",
        "Multimodal de Meta, rápido",
        "foundry",
        "Llama-4-Maverick-17B-128E-Instruct-FP8",
        _p(0.25, 1.00, max_tokens=TEXT_MAX_TOKENS),
        fabricante="Meta",
        vision=True,
    ),
    ModelSpec(
        "llama-4-scout",
        "Llama 4 Scout",
        "El pequeño de Llama 4",
        "foundry",
        "Llama-4-Scout-17B-16E-Instruct",
        _p(0.20, 0.80, max_tokens=TEXT_MAX_TOKENS),  # aprox.
        fabricante="Meta",
        vision=True,
    ),
    ModelSpec(
        "llama-3.3-70b",
        "Llama 3.3 70B",
        "Solo texto",
        "foundry",
        "Llama-3.3-70B-Instruct",
        _p(0.71, 0.71, max_tokens=TEXT_MAX_TOKENS),
        fabricante="Meta",
    ),
    # ---------------------------------------------------------------- Mistral AI
    ModelSpec(
        "mistral-large-3",
        "Mistral Large 3",
        "El grande de Mistral",
        "foundry",
        "Mistral-Large-3",
        _p(0.50, 1.50, max_tokens=TEXT_MAX_TOKENS),
        fabricante="Mistral AI",
        vision=True,
    ),
    ModelSpec(
        "mistral-medium-3.5",
        "Mistral Medium 3.5",
        "Gama media, la versión más nueva",
        "foundry",
        "mistral-medium-3-5",
        _p(1.50, 7.50, max_tokens=TEXT_MAX_TOKENS),
        fabricante="Mistral AI",
        vision=True,
    ),
    ModelSpec(
        "mistral-medium-3",
        "Mistral Medium 3",
        "Gama media, rápido",
        "foundry",
        "mistral-medium-2505",
        _p(0.40, 2.00, max_tokens=TEXT_MAX_TOKENS),  # aprox.
        fabricante="Mistral AI",
        vision=True,
    ),
    ModelSpec(
        "mistral-small-3.1",
        "Mistral Small 3.1",
        "Pequeño y muy barato",
        "foundry",
        "mistral-small-2503",
        _p(0.10, 0.30, max_tokens=TEXT_MAX_TOKENS),  # aprox.
        fabricante="Mistral AI",
        vision=True,
    ),
    ModelSpec(
        "codestral",
        "Codestral",
        "Hecho para programar, solo texto",
        "foundry",
        "Codestral-2501",
        _p(0.30, 0.90, max_tokens=TEXT_MAX_TOKENS),
        fabricante="Mistral AI",
    ),
    # ---------------------------------------------------------------- xAI
    ModelSpec(
        "grok-4.1-fast",
        "Grok 4.1 Fast",
        "Sin razonamiento, muy rápido",
        "foundry",
        "grok-4-1-fast-non-reasoning",
        _p(0.20, 0.50, max_tokens=TEXT_MAX_TOKENS),
        fabricante="xAI",
        vision=True,
    ),
    ModelSpec(
        "grok-4.1-fast-reasoning",
        "Grok 4.1 Fast",
        "Con razonamiento",
        "foundry",
        "grok-4-1-fast-reasoning",
        _p(0.20, 0.50, max_tokens=REASONING_MAX_TOKENS),
        fabricante="xAI",
        vision=True,
        razona=True,
    ),
    ModelSpec(
        "grok-4.20",
        "Grok 4.20",
        "Sin razonamiento",
        "foundry",
        "grok-4-20-non-reasoning",
        _p(1.25, 2.50, max_tokens=TEXT_MAX_TOKENS),
        fabricante="xAI",
        vision=True,
    ),
    ModelSpec(
        "grok-4.20-reasoning",
        "Grok 4.20",
        "Con razonamiento",
        "foundry",
        "grok-4-20-reasoning",
        _p(1.25, 2.50, max_tokens=REASONING_MAX_TOKENS),
        fabricante="xAI",
        vision=True,
        razona=True,
    ),
    ModelSpec(
        "grok-4.3",
        "Grok 4.3",
        "Razona antes de responder",
        "foundry",
        "grok-4.3",
        _p(1.25, 2.50, max_tokens=REASONING_MAX_TOKENS),
        fabricante="xAI",
        vision=True,
        razona=True,
    ),
    ModelSpec(
        "grok-4.6",
        "Grok 4.6",
        "El más nuevo de xAI, razona",
        "foundry",
        "grok-4.6",
        _p(2.00, 6.00, max_tokens=REASONING_MAX_TOKENS),
        fabricante="xAI",
        vision=True,
        razona=True,
        oculto=True,  # deployment con 404 intermitentes (1 de cada 3 llamadas) y 42 s por jugada
    ),
    # ---------------------------------------------------------------- DeepSeek
    ModelSpec(
        "deepseek-v4.1-flash",
        "DeepSeek V4.1 Flash",
        "Rápido y con visión",
        "foundry",
        "DeepSeek-V4.1-Flash-1017",
        _p(0.30, 1.20, max_tokens=TEXT_MAX_TOKENS),
        fabricante="DeepSeek",
        vision=True,
    ),
    ModelSpec(
        "deepseek-v4-pro",
        "DeepSeek V4 Pro",
        "El grande de DeepSeek, solo texto",
        "foundry",
        "DeepSeek-V4-Pro",
        _p(1.74, 3.48, max_tokens=TEXT_MAX_TOKENS),
        fabricante="DeepSeek",
    ),
    ModelSpec(
        "deepseek-v4-flash",
        "DeepSeek V4 Flash",
        "Rápido y barato, solo texto",
        "foundry",
        "DeepSeek-V4-Flash",
        _p(0.19, 0.51, max_tokens=TEXT_MAX_TOKENS),
        fabricante="DeepSeek",
    ),
    ModelSpec(
        "deepseek-v3.2",
        "DeepSeek V3.2",
        "Solo texto",
        "foundry",
        "DeepSeek-V3.2",
        _p(0.58, 1.68, max_tokens=TEXT_MAX_TOKENS),
        fabricante="DeepSeek",
    ),
    # ---------------------------------------------------------------- Moonshot AI
    ModelSpec(
        "kimi-k2.5",
        "Kimi K2.5",
        "Razona y ve imágenes",
        "foundry",
        "Kimi-K2.5",
        _p(0.60, 3.00, max_tokens=REASONING_MAX_TOKENS),
        fabricante="Moonshot AI",
        vision=True,
        razona=True,
    ),
    ModelSpec(
        "kimi-k2.6",
        "Kimi K2.6",
        "Razona a fondo, lento",
        "foundry",
        "Kimi-K2.6",
        _p(0.95, 4.00, max_tokens=REASONING_MAX_TOKENS),
        fabricante="Moonshot AI",
        vision=True,
        ajedrez=False,  # en ajedrez: 68 s y 5 céntimos por jugada, y 2 de 6 jugadas de respaldo
        razona=True,
    ),
    ModelSpec(
        "kimi-k2.7-code",
        "Kimi K2.7 Code",
        "Pensado para código, razona",
        "foundry",
        "Kimi-K2.7-Code",
        _p(0.95, 4.00, max_tokens=REASONING_MAX_TOKENS),
        fabricante="Moonshot AI",
        vision=True,
        razona=True,
    ),
    ModelSpec(
        "kimi-k3.1-pro",
        "Kimi K3.1 Pro",
        "El más nuevo de Moonshot, razona",
        "azure",  # aún no responde por el endpoint de inferencia de modelos, solo por la API v1
        "Kimi-K3.1-Pro",
        _p(3.00, 15.00, max_tokens=REASONING_MAX_TOKENS, structured_output="off", supports_temperature=False),
        fabricante="Moonshot AI",
        razona=True,
        oculto=True,  # 404 intermitentes y casi 2 céntimos por jugada (medio dólar por partida)
    ),
    # ---------------------------------------------------------------- Microsoft
    ModelSpec(
        "mai-thinking-1",
        "MAI-Thinking-1",
        "Modelo propio de Microsoft, razona",
        "foundry",
        "MAI-Thinking-1",
        _p(2.00, 8.00, max_tokens=REASONING_MAX_TOKENS),
        fabricante="Microsoft",
        razona=True,
    ),
    # ---------------------------------------------------------------- Cohere
    ModelSpec(
        "command-a",
        "Command A",
        "Cohere, solo texto",
        "foundry",
        "cohere-command-a",
        _p(2.50, 10.00, max_tokens=TEXT_MAX_TOKENS),
        fabricante="Cohere",
    ),
    # ---------------------------------------------------------------- sin cuota: ocultos hasta que la haya
    ModelSpec(
        "gpt-6-luna",
        "GPT-6 Luna",
        "Sin razonamiento",
        "azure",
        "gpt-6-luna",
        _p(0.10, 0.50, reasoning_effort="none", max_tokens=400, supports_temperature=False),
        vision=True,
        oculto=True,
    ),
    ModelSpec(
        "gpt-6-luna-high",
        "GPT-6 Luna",
        "Razonamiento alto",
        "azure",
        "gpt-6-luna",
        _p(0.10, 0.50, reasoning_effort="high", max_tokens=16000, supports_temperature=False),
        vision=True,
        razona=True,
        oculto=True,
    ),
    ModelSpec(
        "gpt-6.1-terra",
        "GPT-6.1 Terra",
        "El más fuerte de la familia GPT en Foundry",
        "azure",
        "gpt-6.1-terra",
        _p(1.00, 5.00, max_tokens=16000, supports_temperature=False),
        vision=True,
        razona=True,
        oculto=True,
    ),
    ModelSpec(
        "claude-haiku-5-5",
        "Claude Haiku 5.5",
        "Anthropic en Foundry, esfuerzo bajo",
        "claude",
        "claude-haiku-5-5",
        _p(0.10, 0.50, effort="low", max_tokens=4000),
        fabricante="Anthropic",
        vision=True,
        oculto=True,
    ),
)

CATALOG_BY_ID = {m.id: m for m in MODEL_CATALOG}


def model_label(model_id: str, fallback: str | None = None) -> str:
    """«Etiqueta, descripción» para mostrar el rival (o `fallback` si no está en el catálogo)."""
    spec = CATALOG_BY_ID.get(model_id)
    if spec is None:
        return fallback or model_id
    return f"{spec.etiqueta}, {spec.descripcion[:1].lower()}{spec.descripcion[1:]}"


def rival_label(model_id: str) -> str:
    """Nombre corto del rival para el ranking: la etiqueta, y si hay dos con la misma, «razonando»."""
    spec = CATALOG_BY_ID.get(model_id)
    if spec is None:
        return model_id
    twins = [m for m in MODEL_CATALOG if m.etiqueta == spec.etiqueta]
    return f"{spec.etiqueta} razonando" if len(twins) > 1 and spec.razona else spec.etiqueta


def active_model_ids(settings: Settings) -> list[str]:
    """Ids de LLM_MODELS, con `todos` expandido a los no ocultos (en el orden del catálogo)."""
    wanted = list(settings.llm_models)
    if ALL_MODELS in wanted:
        expanded = [m.id for m in MODEL_CATALOG if not m.oculto]
        wanted = expanded + [w for w in wanted if w != ALL_MODELS and w not in expanded]
    return list(dict.fromkeys(wanted))


def _build(spec: ModelSpec, settings: Settings) -> LLMProvider:
    opts = dict(spec.opciones)
    if spec.proveedor == "azure":
        from .llm.azure import AzureOpenAIProvider

        endpoint = os.environ.get(spec.endpoint_env) if spec.endpoint_env else settings.azure_endpoint
        if not endpoint:
            raise RuntimeError(f"falta {spec.endpoint_env or 'AZURE_OPENAI_ENDPOINT'}")
        opts.setdefault("structured_output", "on")
        return AzureOpenAIProvider(
            endpoint,
            spec.deployment,
            api_version=settings.azure_api_version,
            api_key=settings.azure_api_key,
            auth=settings.azure_auth,
            name=spec.id,
            **opts,
        )
    if spec.proveedor == "foundry":
        from .llm.foundry import FoundryInferenceProvider, inference_endpoint

        endpoint = inference_endpoint(settings.azure_endpoint, os.environ.get("AZURE_AI_INFERENCE_ENDPOINT"))
        if not endpoint:
            raise RuntimeError("falta AZURE_AI_INFERENCE_ENDPOINT (o AZURE_OPENAI_ENDPOINT de la misma cuenta)")
        return FoundryInferenceProvider(endpoint, spec.deployment, auth=settings.azure_auth, name=spec.id, **opts)
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
    """Proveedores de los modelos activados en LLM_MODELS (los que fallen al crearse se omiten)."""
    out: dict[str, LLMProvider] = {}
    for model_id in active_model_ids(settings):
        spec = CATALOG_BY_ID.get(model_id)
        if spec is None:
            log.warning("LLM_MODELS: %s no está en el catálogo", model_id)
            continue
        try:
            out[model_id] = _build(spec, settings)
        except Exception as exc:  # noqa: BLE001 - un modelo mal configurado no tumba la API
            log.warning("No se pudo crear el modelo %s: %s", model_id, exc)
    return out


def catalog_row(spec: ModelSpec, available: bool, default: str | None) -> dict[str, Any]:
    return {
        "id": spec.id,
        "etiqueta": spec.etiqueta,
        "descripcion": spec.descripcion,
        "proveedor": spec.fabricante,
        "fabricante": spec.fabricante,
        "razona": spec.razona,
        "vision": spec.vision,
        "disponible": available,
        "por_defecto": spec.id == default,
    }


def model_catalog(
    models: dict[str, LLMProvider], default: str | None, *, only: set[str] | None = None
) -> list[dict[str, Any]]:
    """Catálogo para el frontend: los no ocultos y los ocultos que estén activos. `only` filtra por id.

    Los modelos activos que no están en el catálogo (el mock) van al final.
    """
    rows = [
        catalog_row(m, m.id in models, default)
        for m in MODEL_CATALOG
        if (not m.oculto or m.id in models) and (only is None or m.id in only)
    ]
    for model_id, provider in models.items():
        if model_id not in CATALOG_BY_ID:
            rows.append(
                {
                    "id": model_id,
                    "etiqueta": provider.name,
                    "descripcion": "Modelo local de pruebas",
                    "proveedor": provider.kind,
                    "fabricante": provider.kind,
                    "razona": False,
                    "vision": hasattr(provider, "locate"),
                    "disponible": True,
                    "por_defecto": model_id == default,
                }
            )
    return rows
