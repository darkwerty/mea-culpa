from __future__ import annotations

from ..config import Settings, get_settings
from .base import LLMProvider, LLMResult
from .mock import MockProvider, ScriptedProvider


def get_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.llm_provider == "azure":
        from .azure import AzureOpenAIProvider

        return AzureOpenAIProvider(settings)
    return MockProvider()


__all__ = ["LLMProvider", "LLMResult", "MockProvider", "ScriptedProvider", "get_provider"]
