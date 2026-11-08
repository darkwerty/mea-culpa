"""Proveedor local: modelos servidos por Ollama a través de su API compatible con OpenAI.

`http://localhost:11434/v1` por defecto (o `OLLAMA_BASE_URL`). Ollama ignora la API key, pero el
cliente `openai` exige una. La salida estructurada (`response_format` con JSON Schema) la admiten
las versiones recientes de Ollama; con `structured_output="auto"`, si el servidor la rechaza se pasa
a pedir la jugada en texto.
"""

from __future__ import annotations

import os
from typing import Any

from .openai_compat import OpenAIChatProvider

DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"


class OllamaProvider(OpenAIChatProvider):
    kind = "ollama"

    def __init__(
        self, model: str, *, base_url: str | None = None, http_client=None, timeout: float = 600.0, **options: Any
    ) -> None:
        from openai import OpenAI

        base_url = (base_url or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        extra = {"http_client": http_client} if http_client is not None else {}
        client = OpenAI(base_url=base_url + "/", api_key="ollama", timeout=timeout, max_retries=1, **extra)
        options.setdefault("max_tokens_field", "max_tokens")
        self.base_url = base_url
        super().__init__(client, model, **options)
