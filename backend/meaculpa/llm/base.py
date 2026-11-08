"""Interfaz mínima de proveedor LLM: una llamada de chat con salida estructurada (pydantic)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResult(Generic[T]):
    parsed: T | None
    raw_text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    model: str
    error: str | None = None


class LLMProvider(Protocol):
    name: str  # nombre del modelo / deployment, para métricas
    kind: str  # "mock" | "azure" | ...

    def structured(self, messages: list[dict[str, str]], schema: type[T], temperature: float) -> LLMResult[T]: ...
