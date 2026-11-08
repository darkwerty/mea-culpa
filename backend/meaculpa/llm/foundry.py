"""Modelos de Foundry que no son de OpenAI (Meta, Mistral, xAI, DeepSeek, Moonshot, Microsoft, Cohere).

Van por el endpoint de inferencia de modelos de la cuenta de Foundry (Azure AI Model Inference):

    https://<cuenta>.services.ai.azure.com/models/chat/completions?api-version=2024-05-01-preview

Es compatible con Chat Completions de OpenAI, así que se usa el SDK `openai` con esa URL base, la misma
autenticación de Entra ID (identidad administrada, rol «Azure AI User» en la cuenta) y `model` = nombre del
deployment. Diferencias con Azure OpenAI que se tienen en cuenta aquí:

- No todos aceptan `response_format` con esquema JSON: la jugada se pide en texto («Jugada: <SAN>») y la
  respuesta del juego de fotos en JSON dentro del texto (ver geo/vision.py).
- El límite de salida es `max_tokens` (no `max_completion_tokens`).
- Algunos modelos de razonamiento (DeepSeek, Phi) devuelven el razonamiento entre <think>...</think>
  dentro del contenido: se quita antes de buscar la respuesta.
"""

from __future__ import annotations

import re
from typing import Any

from .azure import AUTH_MODES, COGNITIVE_SCOPE, entra_token_provider
from .base import LLMResult
from .openai_compat import OpenAIChatProvider

INFERENCE_API_VERSION = "2024-05-01-preview"
REQUEST_TIMEOUT_S = 120.0  # un modelo lento no puede dejar colgada una jugada

_THINK = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)
_CLIENTS: dict[tuple, Any] = {}


def strip_reasoning(text: str | None) -> str:
    """Quita los bloques <think> (y uno sin cerrar si la respuesta se cortó)."""
    return _THINK.sub("", text or "").strip()


def inference_endpoint(openai_endpoint: str | None, override: str | None = None) -> str | None:
    """https://x.openai.azure.com/ (o .cognitiveservices.azure.com) → https://x.services.ai.azure.com."""
    if override:
        return override.rstrip("/").removesuffix("/models")
    if not openai_endpoint:
        return None
    m = re.match(r"https://([a-z0-9-]+)\.(openai|cognitiveservices|services\.ai)\.azure\.com", openai_endpoint.strip())
    return f"https://{m.group(1)}.services.ai.azure.com" if m else None


def _client(endpoint: str, credential: Any, api_version: str, http_client: Any = None) -> Any:
    from openai import OpenAI

    extra = {"http_client": http_client} if http_client is not None else {}
    return OpenAI(
        base_url=f"{endpoint}/models",
        api_key=credential,
        default_query={"api-version": api_version},
        timeout=REQUEST_TIMEOUT_S,
        max_retries=1,
        **extra,
    )


class FoundryInferenceProvider(OpenAIChatProvider):
    kind = "foundry"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        *,
        auth: str = "default",
        api_key: str | None = None,
        api_version: str = INFERENCE_API_VERSION,
        token_provider=None,
        http_client=None,
        **options: Any,
    ) -> None:
        """`options`: name, max_tokens, supports_temperature, price_input_per_m, price_output_per_m."""
        if not endpoint or not deployment:
            raise RuntimeError("El endpoint de inferencia de Foundry requiere endpoint y deployment")
        auth = (auth or "default").lower()
        if auth not in AUTH_MODES:
            raise ValueError(f"auth={auth!r} no es válido; opciones: {', '.join(AUTH_MODES)}")
        endpoint = endpoint.rstrip("/")
        if api_key:
            credential: Any = api_key
        elif token_provider is not None:
            credential = token_provider
        else:
            credential = entra_token_provider(auth, COGNITIVE_SCOPE)
        if http_client is not None or token_provider is not None:
            client = _client(endpoint, credential, api_version, http_client)
        else:
            key = (endpoint, api_version, auth, api_key or "")
            client = _CLIENTS.get(key)
            if client is None:
                client = _CLIENTS[key] = _client(endpoint, credential, api_version)
        options.setdefault("structured_output", "off")
        options.setdefault("max_tokens_field", "max_tokens")
        self.endpoint = endpoint
        super().__init__(client, deployment, **options)

    def text(self, messages: list[dict[str, Any]], temperature: float | None = None) -> LLMResult[Any]:
        res = super().text(messages, temperature)
        res.raw_text = strip_reasoning(res.raw_text)
        return res
