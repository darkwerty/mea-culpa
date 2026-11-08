"""Proveedor real: modelos de chat desplegados en Microsoft Foundry / Azure OpenAI.

Se usa el SDK oficial `openai`:
- Por defecto, la API v1 de Azure OpenAI (`{endpoint}/openai/v1/`) con el cliente `OpenAI`,
  que no necesita `api-version`.
- Si se define `api_version`, se usa el cliente `AzureOpenAI` clásico.

Varias implementaciones (deployments) a la vez: cada `AzureOpenAIProvider` apunta a un deployment
por nombre, y los que comparten endpoint y autenticación comparten también el cliente HTTP.

Autenticación (`auth`):
- "default": Entra ID con `DefaultAzureCredential` (identidad administrada en Azure, `az login` en local).
- "cli": Entra ID con `AzureCliCredential` (solo la sesión de `az login`; lo más directo en local).
- "key": API key (plan B). Si se pasa `api_key`, se usa aunque `auth` diga otra cosa.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ..config import Settings
from .openai_compat import OpenAIChatProvider

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
AUTH_MODES = ("default", "cli", "key")


@lru_cache(maxsize=4)
def _credential(auth: str) -> Any:
    if auth == "cli":
        from azure.identity import AzureCliCredential

        return AzureCliCredential()
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


@lru_cache(maxsize=8)
def entra_token_provider(auth: str, scope: str):
    """Callable que devuelve un token de Entra ID válido (se refresca solo). Uno por (auth, ámbito)."""
    from azure.identity import get_bearer_token_provider

    return get_bearer_token_provider(_credential(auth), scope)


_CLIENTS: dict[tuple, Any] = {}


def _openai_client(endpoint: str, api_version: str | None, credential: Any, http_client: Any = None) -> Any:
    from openai import AzureOpenAI, OpenAI

    extra = {"http_client": http_client} if http_client is not None else {}
    if api_version:
        auth = {"api_key": credential} if isinstance(credential, str) else {"azure_ad_token_provider": credential}
        return AzureOpenAI(azure_endpoint=endpoint, api_version=api_version, **auth, **extra)
    # API v1: el cliente estándar `OpenAI`; api_key acepta un callable que devuelve
    # el token de Entra ID en cada petición.
    return OpenAI(base_url=f"{endpoint}/openai/v1/", api_key=credential, **extra)


class AzureOpenAIProvider(OpenAIChatProvider):
    kind = "azure"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        *,
        api_version: str | None = None,
        api_key: str | None = None,
        auth: str = "default",
        token_provider=None,
        http_client=None,
        **options: Any,
    ) -> None:
        """`token_provider` y `http_client` solo se inyectan en tests.

        `options`: name, reasoning_effort, max_tokens, supports_temperature, structured_output,
        price_input_per_m, price_output_per_m (ver `OpenAIChatProvider`).
        """
        if not endpoint or not deployment:
            raise RuntimeError("Azure OpenAI requiere endpoint y deployment")
        auth = (auth or "default").lower()
        if auth not in AUTH_MODES:
            raise ValueError(f"auth={auth!r} no es válido; opciones: {', '.join(AUTH_MODES)}")
        endpoint = endpoint.rstrip("/")
        if endpoint.endswith("/openai/v1"):
            endpoint = endpoint[: -len("/openai/v1")]

        if api_key:
            credential: Any = api_key
        elif token_provider is not None:
            credential = token_provider
        elif auth == "key":
            raise RuntimeError("auth=key requiere una API key (AZURE_OPENAI_API_KEY)")
        else:
            credential = entra_token_provider(auth, COGNITIVE_SCOPE)

        if http_client is not None or token_provider is not None:
            client = _openai_client(endpoint, api_version, credential, http_client)
        else:
            # Los deployments del mismo recurso y la misma autenticación comparten cliente.
            key = (endpoint, api_version, auth, api_key or "")
            client = _CLIENTS.get(key)
            if client is None:
                client = _CLIENTS[key] = _openai_client(endpoint, api_version, credential)
        self.endpoint = endpoint
        super().__init__(client, deployment, **options)

    @classmethod
    def from_settings(cls, settings: Settings, token_provider=None, http_client=None) -> AzureOpenAIProvider:
        if not settings.azure_endpoint or not settings.azure_deployment:
            raise RuntimeError("LLM_PROVIDER=azure requiere AZURE_OPENAI_ENDPOINT y AZURE_OPENAI_DEPLOYMENT")
        return cls(
            settings.azure_endpoint,
            settings.azure_deployment,
            api_version=settings.azure_api_version,
            api_key=settings.azure_api_key,
            auth=settings.azure_auth,
            token_provider=token_provider,
            http_client=http_client,
            reasoning_effort=settings.azure_reasoning_effort,
            max_tokens=settings.llm_max_tokens,
            supports_temperature=settings.azure_supports_temperature,
            structured_output="on",
            price_input_per_m=settings.price_input_per_m,
            price_output_per_m=settings.price_output_per_m,
        )


def azure_deployments(
    endpoint: str, deployments: dict[str, dict[str, Any]], **common: Any
) -> dict[str, AzureOpenAIProvider]:
    """Varios deployments del mismo recurso: {nombre: opciones} → {nombre: proveedor}."""
    out: dict[str, AzureOpenAIProvider] = {}
    for name, opts in deployments.items():
        opts = dict(opts)
        deployment = opts.pop("deployment", name)
        out[name] = AzureOpenAIProvider(endpoint, deployment, **{**common, **opts, "name": name})
    return out
