from __future__ import annotations

from ..config import Settings, get_settings
from .base import LLMProvider, LLMResult, cost_usd
from .mock import MockProvider, ScriptedProvider


def get_provider(settings: Settings | None = None) -> LLMProvider:
    """Proveedor de la demo, elegido por LLM_PROVIDER (mock | azure | claude | ollama)."""
    settings = settings or get_settings()
    if settings.llm_provider == "azure":
        from .azure import AzureOpenAIProvider

        return AzureOpenAIProvider.from_settings(settings)
    if settings.llm_provider == "claude":
        import os

        from .claude import ClaudeFoundryProvider

        return ClaudeFoundryProvider(
            os.environ.get("ANTHROPIC_FOUNDRY_DEPLOYMENT", "claude-haiku-5-5"),
            resource=os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or None,
            base_url=os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL") or None,
            api_key=os.environ.get("ANTHROPIC_FOUNDRY_API_KEY") or None,
            auth=settings.azure_auth,
            max_tokens=settings.llm_max_tokens,
            effort=os.environ.get("ANTHROPIC_FOUNDRY_EFFORT") or None,
            price_input_per_m=settings.price_input_per_m,
            price_output_per_m=settings.price_output_per_m,
        )
    if settings.llm_provider == "ollama":
        import os

        from .ollama import OllamaProvider

        return OllamaProvider(
            os.environ.get("OLLAMA_MODEL", "lfm3:1.7b"),
            max_tokens=settings.llm_max_tokens,
            price_input_per_m=0.0,
            price_output_per_m=0.0,
        )
    return MockProvider()


__all__ = ["LLMProvider", "LLMResult", "MockProvider", "ScriptedProvider", "cost_usd", "get_provider"]
