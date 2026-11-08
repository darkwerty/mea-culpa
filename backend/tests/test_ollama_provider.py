"""Ollama (API compatible con OpenAI en localhost:11434/v1), sin red: transporte HTTP falso."""

from __future__ import annotations

import json

import chess
import httpx2

from meaculpa.llm.ollama import OllamaProvider
from meaculpa.llm_player import LLMOpponent


def _completion(content: str) -> dict:
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": "lfm3:1.7b",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 9, "total_tokens": 209},
    }


def test_structured_request_to_local_ollama():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_completion('{"move": "e4"}'))

    p = OllamaProvider("lfm3:1.7b", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_tokens=64)
    record = LLMOpponent(p, elo_mode=True).play(chess.Board())
    assert record.final_uci == "e2e4" and record.cost_usd == 0.0
    req = seen[0]
    assert str(req.url) == "http://localhost:11434/v1/chat/completions"
    body = json.loads(req.content)
    assert body["model"] == "lfm3:1.7b" and body["max_tokens"] == 64
    assert body["response_format"]["type"] == "json_schema"
    assert "temperature" not in body


def test_falls_back_to_text_when_server_rejects_response_format():
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx2.Response(400, json={"error": {"message": "response_format not supported"}})
        return httpx2.Response(200, json=_completion("Creo que 1. d4 es lo mejor.\nJugada: d4"))

    p = OllamaProvider("viejo", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    record = LLMOpponent(p).play(chess.Board())
    assert record.final_uci == "d2d4"
    assert len(record.attempts) == 1 and record.attempts[0].modo == "texto"  # el 400 no cuenta como intento
    assert not p.supports_structured  # la siguiente jugada ya va directa en texto
    assert "Jugada:" in seen[-1]["messages"][0]["content"]
