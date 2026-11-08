"""Proveedor Claude en Microsoft Foundry (Messages API de Anthropic).

Según Microsoft Learn («Deploy and use Claude models in Microsoft Foundry», act. 2026-09-22):
- URL base del recurso: `https://<recurso>.services.ai.azure.com/anthropic` (las peticiones van a
  `/anthropic/v1/messages`); el parámetro `model` es el NOMBRE DEL DEPLOYMENT.
- Autenticación: Entra ID con ámbito `https://ai.azure.com/.default` (rol Cognitive Services User),
  o la API key del recurso.
- SDK oficial: `anthropic.AnthropicFoundry(resource=... | base_url=..., azure_ad_token_provider=... | api_key=...)`.

Nunca se envían `temperature`, `top_p` ni `top_k`: Claude Haiku/Sonnet 5.5 devuelven 400 con valores
distintos del predeterminado, y con la versión de API 2026-10-21 (la que manda el SDK) con cualquier
valor. El argumento `temperature` de la interfaz se ignora a propósito.

Salida estructurada con `messages.parse(output_format=<modelo pydantic>)` (→ `output_config.format`).
`effort` (output_config.effort) y `thinking` solo se envían si se configuran. Los turnos del asistente
se reenvían tal cual (bloques `thinking` firmados incluidos) para no romper el «preserved thinking».
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from .azure import AUTH_MODES, entra_token_provider
from .base import CLAUDE_EFFORTS, LLMResult, T, check_choice

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
DEFAULT_MAX_TOKENS = 4096  # el pensamiento adaptativo cuenta dentro de max_tokens
THINKING_MODES = ("adaptive", "disabled")


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Mensajes estilo OpenAI → (system, messages) de la Messages API.

    Un turno del asistente con `native` (bloques devueltos por la API) se reenvía sin tocar.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            system_parts.append(str(m.get("content") or ""))
            continue
        content: Any = m.get("native") if role == "assistant" and m.get("native") else str(m.get("content") or "")
        out.append({"role": role, "content": content})
    return ("\n\n".join(system_parts) or None), out


class ClaudeFoundryProvider:
    kind = "claude"
    supports_structured = True

    def __init__(
        self,
        deployment: str,
        *,
        resource: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        auth: str = "default",
        token_provider=None,
        http_client=None,
        name: str | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        thinking: str | None = None,
        price_input_per_m: float = 0.0,
        price_output_per_m: float = 0.0,
        timeout: float = 600.0,
    ) -> None:
        from anthropic import AnthropicFoundry

        if not deployment:
            raise RuntimeError("Claude en Foundry requiere el nombre del deployment")
        if not resource and not base_url:
            raise RuntimeError("Claude en Foundry requiere `recurso` (ANTHROPIC_FOUNDRY_RESOURCE) o `base_url`")
        auth = (auth or "default").lower()
        if auth not in AUTH_MODES:
            raise ValueError(f"auth={auth!r} no es válido; opciones: {', '.join(AUTH_MODES)}")
        kwargs: dict[str, Any] = {"timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        else:
            kwargs["resource"] = resource
        if api_key:
            kwargs["api_key"] = api_key
        elif token_provider is not None:
            kwargs["azure_ad_token_provider"] = token_provider
        elif auth == "key":
            raise RuntimeError("auth=key requiere una API key (ANTHROPIC_FOUNDRY_API_KEY)")
        else:
            kwargs["azure_ad_token_provider"] = entra_token_provider(auth, FOUNDRY_SCOPE)
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = AnthropicFoundry(**kwargs)
        self.model = deployment
        self.name = name or deployment
        self.max_tokens = int(max_tokens or DEFAULT_MAX_TOKENS)
        self.effort = check_choice(effort, CLAUDE_EFFORTS, "effort")
        self.thinking = check_choice(thinking, THINKING_MODES, "thinking")
        self.price_input_per_m = float(price_input_per_m)
        self.price_output_per_m = float(price_output_per_m)

    def describe(self) -> dict[str, Any]:
        return {
            "proveedor": self.kind,
            "modelo": self.model,
            "effort": self.effort,
            "thinking": self.thinking,
            "max_tokens": self.max_tokens,
        }

    def _request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        system, msgs = to_anthropic(messages)
        req: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": msgs}
        if system:
            req["system"] = system
        if self.effort:
            req["output_config"] = {"effort": self.effort}
        if self.thinking:
            req["thinking"] = {"type": self.thinking}
        return req

    @staticmethod
    def _usage(body: dict[str, Any]) -> tuple[int, int]:
        u = body.get("usage") or {}
        tin = int(u.get("input_tokens") or 0)
        tin += int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
        return tin, int(u.get("output_tokens") or 0)

    @staticmethod
    def _text(body: dict[str, Any]) -> str:
        return "".join(b.get("text", "") for b in body.get("content") or [] if b.get("type") == "text")

    def _result(self, body: dict[str, Any], latency: float, parsed: Any, error: str | None) -> LLMResult[Any]:
        tin, tout = self._usage(body)
        stop = body.get("stop_reason")
        if stop == "refusal":
            error = f"rechazo: {(body.get('stop_details') or {}).get('category')}"
            parsed = None
        elif stop == "max_tokens" and parsed is None and not error:
            error = "max_tokens agotado antes de responder"
        return LLMResult(
            parsed=parsed,
            raw_text=self._text(body),
            input_tokens=tin,
            output_tokens=tout,
            latency_ms=latency,
            model=body.get("model") or self.name,
            error=error,
            native=body.get("content") or None,
        )

    def structured(
        self, messages: list[dict[str, Any]], schema: type[T], temperature: float | None = None
    ) -> LLMResult[T]:
        del temperature  # nunca se envía a Claude (ver docstring del módulo)
        from anthropic import transform_schema

        req = self._request(messages)
        req["output_config"] = {
            **req.get("output_config", {}),
            "format": {"type": "json_schema", "schema": transform_schema(schema)},
        }
        t0 = time.perf_counter()
        # Respuesta en bruto: el uso de tokens se lee siempre, aunque el JSON no cumpla el esquema.
        raw = self._client.messages.with_raw_response.create(**req)
        latency = (time.perf_counter() - t0) * 1000
        body = raw.http_response.json()
        text = self._text(body)
        parsed, error = None, None
        if text.strip():
            try:
                parsed = schema.model_validate_json(text)
            except (ValidationError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
        elif body.get("stop_reason") != "max_tokens":
            error = "respuesta sin bloque de texto"
        return self._result(body, latency, parsed, error)

    def text(self, messages: list[dict[str, Any]], temperature: float | None = None) -> LLMResult[Any]:
        del temperature
        t0 = time.perf_counter()
        raw = self._client.messages.with_raw_response.create(**self._request(messages))
        latency = (time.perf_counter() - t0) * 1000
        return self._result(raw.http_response.json(), latency, None, None)
