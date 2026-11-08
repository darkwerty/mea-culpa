"""Interfaz mínima de proveedor LLM.

Dos llamadas:
- `structured(messages, schema, temperature)`: chat con salida estructurada (pydantic).
- `text(messages, temperature)`: chat con respuesta de texto libre (para modelos sin salida estructurada).

`temperature=None` significa «no enviar el parámetro» (se usa el valor por defecto del proveedor).
Los parámetros propios de cada modelo (reasoning_effort, effort, max_tokens, precios) se fijan al
crear el proveedor, no en cada llamada.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# Valores que acepta `reasoning_effort` en la API de OpenAI (los modelos admiten un subconjunto).
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Valores de `output_config.effort` en la Messages API de Anthropic.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class LLMResult(Generic[T]):
    parsed: T | None
    raw_text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    model: str
    error: str | None = None
    # Turno del asistente tal y como lo devolvió el proveedor (p. ej. bloques de Claude con su
    # `thinking` firmado). Si existe, se reenvía sin tocar en los reintentos.
    native: Any = None


class LLMProvider(Protocol):
    name: str  # nombre del modelo / deployment, para métricas
    kind: str  # "mock" | "azure" | "claude" | "ollama" | ...

    def structured(
        self, messages: list[dict[str, Any]], schema: type[T], temperature: float | None = None
    ) -> LLMResult[T]: ...


def cost_usd(provider: object, input_tokens: int, output_tokens: int) -> float:
    """Coste con los precios por millón de tokens que lleve el proveedor (0 si no los tiene)."""
    pin = float(getattr(provider, "price_input_per_m", 0.0) or 0.0)
    pout = float(getattr(provider, "price_output_per_m", 0.0) or 0.0)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def plain_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Solo `role` y `content` (texto): lo que entiende cualquier API compatible con OpenAI."""
    return [{"role": m["role"], "content": str(m.get("content") or "")} for m in messages]


def check_choice(value: str | None, allowed: tuple[str, ...], field: str) -> str | None:
    if value is None or value == "":
        return None
    v = str(value).strip().lower()
    if v not in allowed:
        raise ValueError(f"{field}={value!r} no es válido; opciones: {', '.join(allowed)}")
    return v
