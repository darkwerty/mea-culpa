"""El proveedor real se prueba sin red: un transporte HTTP falso hace de Azure OpenAI.

Así se comprueba la URL, la cabecera de autenticación (token de Entra ID), qué parámetros se envían
(temperatura solo si se configura, reasoning_effort, max_completion_tokens) y que el JSON de salida
estructurada se convierte en el modelo pydantic, sin gastar un céntimo.
"""

from __future__ import annotations

import dataclasses
import json

import chess
import httpx2
import pytest

from meaculpa.llm import azure as azure_mod
from meaculpa.llm.azure import AzureOpenAIProvider, azure_deployments
from meaculpa.llm_player import LLMOpponent
from meaculpa.schemas import MoveProposal

ENDPOINT = "https://mi-recurso.openai.azure.com/"


def _fake_completion(content: dict | str, model: str = "gpt-test") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": content if isinstance(content, str) else json.dumps(content),
                    "refusal": None,
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132},
    }


def _transport(seen: list, content: dict | str = None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_fake_completion(content or {"move": "e4"}))

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_v1_endpoint_with_entra_token(settings):
    seen: list[httpx2.Request] = []
    cfg = dataclasses.replace(
        settings,
        llm_provider="azure",
        azure_endpoint=ENDPOINT,
        azure_deployment="gpt-test",
        azure_api_version=None,
        azure_api_key=None,
    )
    provider = AzureOpenAIProvider.from_settings(
        cfg, token_provider=lambda: "token-entra", http_client=_transport(seen)
    )
    res = provider.structured([{"role": "user", "content": "hola"}], MoveProposal, temperature=0.4)

    assert isinstance(res.parsed, MoveProposal) and res.parsed.move == "e4"
    assert (res.input_tokens, res.output_tokens) == (120, 12)
    req = seen[0]
    assert str(req.url) == "https://mi-recurso.openai.azure.com/openai/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer token-entra"
    body = json.loads(req.content)
    assert body["model"] == "gpt-test" and body["temperature"] == 0.4
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["move"]
    assert "reasoning_effort" not in body and "max_completion_tokens" not in body

    # Y el rival LLM funciona de punta a punta con el proveedor real (red simulada).
    record = LLMOpponent(provider, temperature=0.4).play(chess.Board())
    assert record.final_uci == "e2e4" and not record.fallback


def test_classic_client_with_api_version_and_key(settings):
    seen: list[httpx2.Request] = []
    cfg = dataclasses.replace(
        settings,
        azure_endpoint="https://mi-recurso.openai.azure.com",
        azure_deployment="gpt-test",
        azure_api_version="2024-10-21",
        azure_api_key="clave-secreta",
        azure_supports_temperature=False,
    )
    provider = AzureOpenAIProvider.from_settings(cfg, http_client=_transport(seen, {"move": "Nf3"}))
    res = provider.structured([{"role": "user", "content": "hola"}], MoveProposal, temperature=0.9)
    assert res.parsed.move == "Nf3"
    req = seen[0]
    assert req.url.path == "/openai/deployments/gpt-test/chat/completions"
    assert req.url.params["api-version"] == "2024-10-21"
    assert req.headers["api-key"] == "clave-secreta"
    assert "temperature" not in json.loads(req.content)  # modelos de razonamiento no la aceptan


def test_temperature_not_sent_by_default_and_reasoning_options_are():
    seen: list[httpx2.Request] = []
    provider = AzureOpenAIProvider(
        ENDPOINT,
        "gpt-6-luna",
        token_provider=lambda: "t",
        http_client=_transport(seen),
        reasoning_effort="none",
        max_tokens=300,
    )
    record = LLMOpponent(provider).play(chess.Board())  # sin temperatura configurada
    body = json.loads(seen[0].content)
    assert "temperature" not in body and "top_p" not in body
    assert body["reasoning_effort"] == "none"
    assert body["max_completion_tokens"] == 300
    assert record.temperature is None and record.final_uci == "e2e4"


def test_invalid_reasoning_effort_is_rejected():
    with pytest.raises(ValueError, match="reasoning_effort"):
        AzureOpenAIProvider(ENDPOINT, "x", token_provider=lambda: "t", reasoning_effort="extremo")


def test_text_mode_extracts_san_from_free_text():
    seen: list[httpx2.Request] = []
    provider = AzureOpenAIProvider(
        ENDPOINT,
        "modelo-sin-json",
        token_provider=lambda: "t",
        http_client=_transport(seen, "Desarrollo el caballo.\nJugada: Nf3"),
        structured_output=False,
    )
    record = LLMOpponent(provider).play(chess.Board())
    assert record.final_uci == "g1f3" and record.attempts[0].modo == "texto"
    assert "response_format" not in json.loads(seen[0].content)


def test_several_deployments_share_one_client_and_cli_credential(monkeypatch):
    created: list[str] = []

    class FakeCliCredential:
        def __init__(self) -> None:
            created.append("cli")

        def get_token(self, *scopes, **kwargs):  # pragma: no cover - no se llama sin red
            raise AssertionError("sin red en los tests")

    monkeypatch.setattr("azure.identity.AzureCliCredential", FakeCliCredential)
    azure_mod._credential.cache_clear()
    azure_mod.entra_token_provider.cache_clear()
    azure_mod._CLIENTS.clear()
    providers = azure_deployments(
        ENDPOINT,
        {
            "luna-none": {"deployment": "gpt-6-luna", "reasoning_effort": "none"},
            "luna-high": {"deployment": "gpt-6-luna", "reasoning_effort": "high"},
            "terra": {"deployment": "gpt-6.1-terra", "max_tokens": 2000},
        },
        auth="cli",
    )
    assert {p.name: p.model for p in providers.values()} == {
        "luna-none": "gpt-6-luna",
        "luna-high": "gpt-6-luna",
        "terra": "gpt-6.1-terra",
    }
    assert providers["luna-high"].reasoning_effort == "high"
    clients = {id(p._client) for p in providers.values()}
    assert len(clients) == 1 and created == ["cli"]
    azure_mod._CLIENTS.clear()
    azure_mod._credential.cache_clear()
    azure_mod.entra_token_provider.cache_clear()
