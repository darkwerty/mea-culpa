"""Base común para APIs de chat compatibles con OpenAI (Azure OpenAI / Foundry, Ollama).

Solo se envían los parámetros configurados explícitamente:
- `temperature`: solo si la llamada la trae (None = valor por defecto del proveedor) y el modelo la admite.
- `reasoning_effort`: solo si se configuró (none/minimal/low/medium/high/xhigh/max; cada modelo admite un subconjunto).
- `max_tokens`: solo si se configuró; por defecto como `max_completion_tokens` (el campo que aceptan los
  modelos de razonamiento), o `max_tokens` para servidores que solo conocen ese.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import ValidationError

from .base import REASONING_EFFORTS, LLMResult, T, check_choice, plain_messages

StructuredMode = Literal["auto", "on", "off"]


class StructuredOutputUnsupported(RuntimeError):
    """El servidor rechazó `response_format`: hay que pedir la jugada en texto."""


def _structured_mode(value: object) -> StructuredMode:
    if value is True or value in ("on", "true", "si", "sí", "yes"):
        return "on"
    if value is False or value in ("off", "false", "no"):
        return "off"
    if value in (None, "auto"):
        return "auto"
    raise ValueError(f"salida_estructurada={value!r}: usa auto, true o false")


class OpenAIChatProvider:
    kind = "openai"

    def __init__(
        self,
        client: Any,
        model: str,
        *,
        name: str | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        max_tokens_field: str = "max_completion_tokens",
        supports_temperature: bool = True,
        structured_output: object = "auto",
        price_input_per_m: float = 0.0,
        price_output_per_m: float = 0.0,
    ) -> None:
        self._client = client
        self.model = model
        self.name = name or model
        self.reasoning_effort = check_choice(reasoning_effort, REASONING_EFFORTS, "reasoning_effort")
        self.max_tokens = int(max_tokens) if max_tokens else None
        self._max_tokens_field = max_tokens_field
        self._supports_temperature = supports_temperature
        self._structured_mode = _structured_mode(structured_output)
        self.price_input_per_m = float(price_input_per_m)
        self.price_output_per_m = float(price_output_per_m)

    # ------------------------------------------------------------ utilidades
    @property
    def supports_structured(self) -> bool:
        return self._structured_mode != "off"

    def generation_kwargs(self, temperature: float | None) -> dict[str, Any]:
        kw: dict[str, Any] = {}
        if temperature is not None and self._supports_temperature:
            kw["temperature"] = temperature
        if self.reasoning_effort is not None:
            kw["reasoning_effort"] = self.reasoning_effort
        if self.max_tokens is not None:
            kw[self._max_tokens_field] = self.max_tokens
        return kw

    def describe(self) -> dict[str, Any]:
        return {
            "proveedor": self.kind,
            "modelo": self.model,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
            "salida_estructurada": self._structured_mode,
        }

    @staticmethod
    def _usage(completion: Any) -> tuple[int, int]:
        usage = getattr(completion, "usage", None)
        if not usage:
            return 0, 0
        return int(usage.prompt_tokens or 0), int(usage.completion_tokens or 0)

    # --------------------------------------------------------------- llamadas
    def structured(
        self, messages: list[dict[str, Any]], schema: type[T], temperature: float | None = None
    ) -> LLMResult[T]:
        if self._structured_mode == "off":
            raise StructuredOutputUnsupported(f"{self.name}: salida estructurada desactivada")
        from openai import BadRequestError, ContentFilterFinishReasonError, LengthFinishReasonError

        kwargs = {
            "model": self.model,
            "messages": plain_messages(messages),
            "response_format": schema,
            **self.generation_kwargs(temperature),
        }
        t0 = time.perf_counter()
        try:
            completion = self._client.chat.completions.parse(**kwargs)
        except BadRequestError as exc:
            if self._structured_mode == "auto" and "response_format" in str(exc):
                self._structured_mode = "off"
                raise StructuredOutputUnsupported(str(exc)) from exc
            raise
        except (ValidationError, LengthFinishReasonError, ContentFilterFinishReasonError, ValueError) as exc:
            # El modelo devolvió algo que no cumple el esquema: se trata como respuesta no parseable.
            elapsed = (time.perf_counter() - t0) * 1000
            completion = getattr(exc, "completion", None)
            tin, tout = self._usage(completion)
            raw = ""
            if completion is not None and completion.choices:
                raw = completion.choices[0].message.content or ""
            return LLMResult(None, raw, tin, tout, elapsed, self.name, f"{type(exc).__name__}: {exc}")
        latency = (time.perf_counter() - t0) * 1000
        msg = completion.choices[0].message
        tin, tout = self._usage(completion)
        return LLMResult(
            parsed=msg.parsed,
            raw_text=msg.content or "",
            input_tokens=tin,
            output_tokens=tout,
            latency_ms=latency,
            model=completion.model or self.name,
            error=f"rechazo: {msg.refusal}" if getattr(msg, "refusal", None) else None,
        )

    def text(self, messages: list[dict[str, Any]], temperature: float | None = None) -> LLMResult[Any]:
        t0 = time.perf_counter()
        completion = self._client.chat.completions.create(
            model=self.model, messages=plain_messages(messages), **self.generation_kwargs(temperature)
        )
        latency = (time.perf_counter() - t0) * 1000
        tin, tout = self._usage(completion)
        choice = completion.choices[0]
        error = "max_tokens agotado antes de responder" if choice.finish_reason == "length" else None
        return LLMResult(None, choice.message.content or "", tin, tout, latency, completion.model or self.name, error)
