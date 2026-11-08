"""El proveedor real se prueba sin red: un transporte HTTP falso hace de Azure OpenAI.

Así se comprueba la URL, la cabecera de autenticación (token de Entra ID) y que el
JSON de salida estructurada se convierte en el modelo pydantic, sin gastar un céntimo.
"""

from __future__ import annotations

import dataclasses
import json

import chess
import httpx2

from meaculpa.llm.azure import AzureOpenAIProvider
from meaculpa.llm_player import LLMOpponent
from meaculpa.schemas import MoveProposal


def _fake_completion(content: dict) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(content), "refusal": None},
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132},
    }


def test_v1_endpoint_with_entra_token(settings):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_fake_completion({"jugada": "e4", "motivo": "centro"}))

    cfg = dataclasses.replace(
        settings,
        llm_provider="azure",
        azure_endpoint="https://mi-recurso.openai.azure.com/",
        azure_deployment="gpt-test",
        azure_api_version=None,
        azure_api_key=None,
    )
    provider = AzureOpenAIProvider(
        cfg, token_provider=lambda: "token-entra", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    res = provider.structured([{"role": "user", "content": "hola"}], MoveProposal, temperature=0.4)

    assert isinstance(res.parsed, MoveProposal) and res.parsed.jugada == "e4"
    assert (res.input_tokens, res.output_tokens) == (120, 12)
    req = seen[0]
    assert str(req.url) == "https://mi-recurso.openai.azure.com/openai/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer token-entra"
    body = json.loads(req.content)
    assert body["model"] == "gpt-test" and body["temperature"] == 0.4
    assert body["response_format"]["type"] == "json_schema"

    # Y el rival LLM funciona de punta a punta con el proveedor real (red simulada).
    record = LLMOpponent(provider, temperature=0.4).play(chess.Board())
    assert record.final_uci == "e2e4" and not record.fallback


def test_classic_client_with_api_version_and_key(settings):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_fake_completion({"jugada": "Nf3", "motivo": "desarrollo"}))

    cfg = dataclasses.replace(
        settings,
        azure_endpoint="https://mi-recurso.openai.azure.com",
        azure_deployment="gpt-test",
        azure_api_version="2024-10-21",
        azure_api_key="clave-secreta",
        azure_supports_temperature=False,
    )
    provider = AzureOpenAIProvider(cfg, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    res = provider.structured([{"role": "user", "content": "hola"}], MoveProposal, temperature=0.9)
    assert res.parsed.jugada == "Nf3"
    req = seen[0]
    assert req.url.path == "/openai/deployments/gpt-test/chat/completions"
    assert req.url.params["api-version"] == "2024-10-21"
    assert req.headers["api-key"] == "clave-secreta"
    assert "temperature" not in json.loads(req.content)  # modelos de razonamiento no la aceptan
