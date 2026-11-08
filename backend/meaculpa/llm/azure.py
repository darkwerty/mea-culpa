"""Proveedor real: modelo de chat desplegado en Microsoft Foundry / Azure OpenAI.

Se usa el SDK oficial `openai`:
- Por defecto, la API v1 de Azure OpenAI (`{endpoint}/openai/v1/`) con el cliente `OpenAI`,
  que no necesita `api-version`.
- Si se define AZURE_OPENAI_API_VERSION, se usa el cliente `AzureOpenAI` clásico.

Autenticación: Entra ID con `DefaultAzureCredential` (identidad administrada en Azure,
`az login` en local). La API key es solo un plan B opcional.
"""

from __future__ import annotations

import time

from pydantic import ValidationError

from ..config import Settings
from .base import LLMResult, T

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


class AzureOpenAIProvider:
    kind = "azure"

    def __init__(self, settings: Settings, token_provider=None, http_client=None) -> None:
        """`token_provider` y `http_client` solo se inyectan en tests."""
        if not settings.azure_endpoint or not settings.azure_deployment:
            raise RuntimeError("LLM_PROVIDER=azure requiere AZURE_OPENAI_ENDPOINT y AZURE_OPENAI_DEPLOYMENT")
        from openai import AzureOpenAI, OpenAI

        self.name = settings.azure_deployment
        self._supports_temperature = settings.azure_supports_temperature
        endpoint = settings.azure_endpoint.rstrip("/")

        if settings.azure_api_key:
            credential: object = settings.azure_api_key
        elif token_provider is not None:
            credential = token_provider
        else:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            credential = get_bearer_token_provider(DefaultAzureCredential(), COGNITIVE_SCOPE)

        extra = {"http_client": http_client} if http_client is not None else {}
        if settings.azure_api_version:
            auth = {"api_key": credential} if isinstance(credential, str) else {"azure_ad_token_provider": credential}
            self._client = AzureOpenAI(azure_endpoint=endpoint, api_version=settings.azure_api_version, **auth, **extra)
        else:
            # API v1: el cliente estándar `OpenAI`; api_key acepta un callable que devuelve
            # el token de Entra ID en cada petición.
            self._client = OpenAI(base_url=f"{endpoint}/openai/v1/", api_key=credential, **extra)

    def structured(self, messages: list[dict[str, str]], schema: type[T], temperature: float) -> LLMResult[T]:
        kwargs: dict = {"model": self.name, "messages": messages, "response_format": schema}
        if self._supports_temperature:
            kwargs["temperature"] = temperature
        from openai import ContentFilterFinishReasonError, LengthFinishReasonError

        t0 = time.perf_counter()
        try:
            completion = self._client.chat.completions.parse(**kwargs)
        except (ValidationError, LengthFinishReasonError, ContentFilterFinishReasonError) as exc:
            # El modelo devolvió algo que no cumple el esquema: se trata como respuesta no parseable.
            elapsed = (time.perf_counter() - t0) * 1000
            return LLMResult(None, "", 0, 0, elapsed, self.name, f"{type(exc).__name__}: {exc}")
        latency = (time.perf_counter() - t0) * 1000
        msg = completion.choices[0].message
        usage = completion.usage
        return LLMResult(
            parsed=msg.parsed,
            raw_text=msg.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency,
            model=completion.model or self.name,
            error=f"rechazo: {msg.refusal}" if msg.refusal else None,
        )
