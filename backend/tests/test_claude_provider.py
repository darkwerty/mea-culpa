"""Claude en Foundry, sin red: un transporte HTTP falso hace de `<recurso>.services.ai.azure.com/anthropic`."""

from __future__ import annotations

import json

import chess
import httpx2
import pytest

from meaculpa.llm.claude import ClaudeFoundryProvider, to_anthropic
from meaculpa.llm_player import LLMOpponent
from meaculpa.schemas import MoveProposal


def _message(text: str, stop: str = "end_turn", tin: int = 300, tout: int = 40) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "stop_reason": stop,
        "stop_sequence": None,
        "content": [{"type": "thinking", "thinking": "", "signature": "firma"}, {"type": "text", "text": text}],
        "usage": {"input_tokens": tin, "output_tokens": tout},
    }


def _provider(seen: list, replies: list[dict], **kw) -> ClaudeFoundryProvider:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=replies.pop(0))

    kw.setdefault("resource", "mi-recurso")
    kw.setdefault("token_provider", lambda: "token-entra")
    return ClaudeFoundryProvider(
        "haiku-deploy", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **kw
    )


def test_messages_api_url_auth_and_no_sampling_params():
    seen: list[httpx2.Request] = []
    p = _provider(seen, [_message('{"move": "e4"}')], effort="low", max_tokens=2000)
    res = p.structured(
        [{"role": "system", "content": "sistema"}, {"role": "user", "content": "hola"}], MoveProposal, 0.7
    )
    assert res.parsed == MoveProposal(move="e4")
    assert (res.input_tokens, res.output_tokens) == (300, 40)
    req = seen[0]
    assert str(req.url) == "https://mi-recurso.services.ai.azure.com/anthropic/v1/messages"
    assert req.headers["authorization"] == "Bearer token-entra"
    assert "anthropic-version" in req.headers
    body = json.loads(req.content)
    assert body["model"] == "haiku-deploy" and body["max_tokens"] == 2000
    assert body["system"] == "sistema" and body["messages"] == [{"role": "user", "content": "hola"}]
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in body  # aunque la llamada traiga temperature=0.7
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == ["move"]
    assert "thinking" not in body  # solo si se configura


def test_api_key_and_base_url():
    seen: list[httpx2.Request] = []
    p = _provider(
        seen,
        [_message('{"move": "d4"}')],
        resource=None,
        token_provider=None,
        base_url="https://otro.services.ai.azure.com/anthropic",
        api_key="clave",
    )
    p.structured([{"role": "user", "content": "x"}], MoveProposal)
    assert seen[0].headers["x-api-key"] == "clave"
    assert str(seen[0].url).startswith("https://otro.services.ai.azure.com/anthropic/v1/messages")


def test_retry_replays_native_blocks_and_counts_tokens_of_bad_json():
    seen: list[httpx2.Request] = []
    p = _provider(
        seen, [_message('{"move": "Ke9"}', tout=55), _message("no es json", tout=7), _message('{"move": "e4"}')]
    )
    record = LLMOpponent(p, max_retries=3).play(chess.Board())
    assert record.final_uci == "e2e4"
    assert [a.valida for a in record.attempts] == [False, False, True]
    assert record.attempts[1].output_tokens == 7 and record.attempts[1].error.startswith("respuesta no parseable")
    second = json.loads(seen[1].content)
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "firma"}
    assert second["messages"][2]["role"] == "user" and "Ke9" in second["messages"][2]["content"]


def test_refusal_and_max_tokens_are_errors():
    seen: list[httpx2.Request] = []
    refusal = _message("")
    refusal["stop_reason"] = "refusal"
    refusal["stop_details"] = {"type": "refusal", "category": "cyber"}
    p = _provider(seen, [refusal, _message("", stop="max_tokens")])
    r1 = p.structured([{"role": "user", "content": "x"}], MoveProposal)
    assert r1.parsed is None and r1.error.startswith("rechazo")
    r2 = p.structured([{"role": "user", "content": "x"}], MoveProposal)
    assert r2.parsed is None and "max_tokens" in r2.error


def test_needs_resource_or_base_url_and_valid_effort():
    with pytest.raises(RuntimeError):
        ClaudeFoundryProvider("d", token_provider=lambda: "t")
    with pytest.raises(ValueError):
        ClaudeFoundryProvider("d", resource="r", token_provider=lambda: "t", effort="none")


def test_to_anthropic_merges_system_and_keeps_order():
    system, msgs = to_anthropic(
        [
            {"role": "system", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "c"},
            {"role": "user", "content": "d"},
        ]
    )
    assert system == "a" and [m["role"] for m in msgs] == ["user", "assistant", "user"]
