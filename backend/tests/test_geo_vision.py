"""Llamadas con imagen a Azure OpenAI y a Claude en Foundry, con clientes simulados (sin red)."""

import base64
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from meaculpa.geo.schemas import GeoGuess
from meaculpa.geo.vision import SYSTEM_PROMPT, USER_PROMPT, MockGeoProvider, locate
from meaculpa.llm.openai_compat import OpenAIChatProvider

IMG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
GOOD = {
    "pais": "Chile",
    "region": "Magallanes",
    "lat": -50.9,
    "lon": -73.0,
    "confianza": 0.6,
    "razonamiento_breve": "Torres",
}


class FakeCompletions:
    def __init__(self, parsed=None, exc=None):
        self.kwargs = None
        self.parsed, self.exc = parsed, exc

    def parse(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        msg = SimpleNamespace(parsed=self.parsed, content=json.dumps(GOOD), refusal=None)
        usage = SimpleNamespace(prompt_tokens=900, completion_tokens=60)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage, model="gpt-6-luna")


def _openai(completions):
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAIChatProvider(
        client,
        "gpt-6-luna",
        name="gpt-6-luna",
        reasoning_effort="none",
        max_tokens=400,
        supports_temperature=False,
        structured_output="on",
    )


def test_openai_sends_only_image_and_generic_prompt():
    comp = FakeCompletions(parsed=GeoGuess(**GOOD))
    res = locate(_openai(comp), IMG, detail="high")
    assert res.parsed.pais == "Chile" and res.input_tokens == 900 and res.error is None
    kw = comp.kwargs
    assert kw["model"] == "gpt-6-luna" and kw["response_format"] is GeoGuess
    assert kw["reasoning_effort"] == "none" and kw["max_completion_tokens"] == 400 and "temperature" not in kw
    system, user = kw["messages"]
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    text, image = user["content"]
    assert text == {"type": "text", "text": USER_PROMPT}
    assert image["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(IMG).decode()


def test_openai_invalid_output_is_an_error_not_a_crash():
    try:
        GeoGuess(**{**GOOD, "lat": 200})
    except ValidationError as exc:
        err = exc
    res = locate(_openai(FakeCompletions(exc=err)), IMG)
    assert res.parsed is None and res.error.startswith("ValidationError")


class FakeRaw:
    def __init__(self, body):
        self.http_response = SimpleNamespace(json=lambda: body)


class FakeMessages:
    def __init__(self, body):
        self.body, self.req = body, None
        self.with_raw_response = self

    def create(self, **req):
        self.req = req
        return FakeRaw(self.body)


def _claude(body):
    from meaculpa.llm.claude import ClaudeFoundryProvider

    p = ClaudeFoundryProvider(
        "claude-haiku-5-5",
        base_url="https://x.services.ai.azure.com/anthropic",
        api_key="k",
        effort="low",
        max_tokens=4000,
    )
    fake = FakeMessages(body)
    p._client = SimpleNamespace(messages=fake)
    return p, fake


def test_claude_sends_image_block_and_schema():
    body = {
        "content": [{"type": "text", "text": json.dumps(GOOD)}],
        "usage": {"input_tokens": 1500, "output_tokens": 80},
        "stop_reason": "end_turn",
        "model": "claude-haiku-5-5",
    }
    p, fake = _claude(body)
    res = locate(p, IMG)
    assert res.parsed.region == "Magallanes" and res.input_tokens == 1500
    req = fake.req
    assert "temperature" not in req and req["system"] == SYSTEM_PROMPT
    img, text = req["messages"][0]["content"]
    assert img["source"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(IMG).decode()}
    assert text["text"] == USER_PROMPT
    assert req["output_config"]["effort"] == "low" and req["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize(
    "body,error",
    [
        (
            {"content": [{"type": "text", "text": '{"pais": "x"}'}], "usage": {}, "stop_reason": "end_turn"},
            "ValidationError",
        ),
        ({"content": [], "usage": {}, "stop_reason": "refusal", "stop_details": {"category": "x"}}, "rechazo"),
    ],
)
def test_claude_bad_outputs(body, error):
    p, _ = _claude(body)
    res = locate(p, IMG)
    assert res.parsed is None and res.error.startswith(error)


def test_mock_is_deterministic_and_valid():
    m = MockGeoProvider()
    a, b = m.locate(IMG), m.locate(IMG)
    assert a.parsed == b.parsed and -90 <= a.parsed.lat <= 90


def test_text_only_provider_rejected():
    with pytest.raises(TypeError):
        locate(SimpleNamespace(kind="ollama"), IMG)
