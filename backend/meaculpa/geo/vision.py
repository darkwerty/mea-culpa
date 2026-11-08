"""Llamada con imagen a los proveedores de Mea culpa.

La interfaz `LLMProvider.structured()` de Mea culpa solo manda texto (`plain_messages` y `to_anthropic`
convierten el contenido en cadena), así que aquí se usa el mismo cliente, deployment, autenticación,
`reasoning_effort`/`effort`, `max_tokens` y precios que ya lleva cada proveedor, pero con un mensaje que
incluye la imagen:

- Azure OpenAI / Foundry (`kind` "azure" u "openai"): Chat Completions con una parte `image_url` en
  base64 y `response_format` = esquema pydantic (`chat.completions.parse`).
- Modelos de Foundry sin salida estructurada (`kind` "foundry", endpoint de inferencia de modelos, o los de la
  API v1 con `structured_output="off"`): la misma imagen y las instrucciones piden un objeto JSON en el texto,
  que se extrae (sin el razonamiento entre <think>) y se valida con el mismo esquema pydantic.
- Claude en Foundry (`kind` "claude"): Messages API con un bloque `image` en base64 y
  `output_config.format` (JSON Schema).
- Mock (`kind` "mock-geo"): no mira la imagen; responde algo determinista para probar todo sin red.

El modelo recibe SOLO la imagen (sin EXIF: las fotos se guardan sin metadatos) y unas instrucciones
genéricas. Nunca el nombre del fichero, el título de Commons, el autor ni la solución.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any

from pydantic import ValidationError

from ..llm.base import LLMResult
from .schemas import GeoGuess

SYSTEM_PROMPT = (
    "Juegas a adivinar dónde se hizo una foto. Mira el paisaje, la vegetación, el clima, la arquitectura, "
    "las carreteras, el sentido de circulación y cualquier otra pista visual. Da tu mejor estimación aunque "
    "no estés seguro. Responde en español con: pais, region (región, provincia o zona), lat y lon en grados "
    "decimales (WGS84), confianza entre 0 y 1, y razonamiento_breve (una o dos frases, máximo 300 caracteres, "
    "con las pistas que has usado)."
)
USER_PROMPT = "¿Dónde se hizo esta foto?"
JSON_PROMPT = (
    " Responde SOLO con un objeto JSON, sin texto antes ni después, con estas claves: "
    '{"pais": "...", "region": "...", "lat": 0.0, "lon": 0.0, "confianza": 0.0, "razonamiento_breve": "..."}'
)
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def _messages_text() -> tuple[str, str]:
    return SYSTEM_PROMPT, USER_PROMPT


def locate(provider: Any, image: bytes, mime: str = "image/jpeg", detail: str = "high") -> LLMResult[GeoGuess]:
    kind = getattr(provider, "kind", "")
    if hasattr(provider, "locate"):  # mock u otros proveedores nativos de imagen
        return provider.locate(image, mime)
    if kind in ("azure", "openai", "foundry"):
        if getattr(provider, "supports_structured", True):
            return _locate_openai(provider, image, mime, detail)
        return _locate_text(provider, image, mime)
    if kind == "claude":
        return _locate_claude(provider, image, mime)
    raise TypeError(f"El proveedor {kind!r} no admite imágenes")


def _locate_openai(provider: Any, image: bytes, mime: str, detail: str) -> LLMResult[GeoGuess]:
    from openai import ContentFilterFinishReasonError, LengthFinishReasonError

    data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    system, user = _messages_text()
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": data_url, "detail": detail}},
            ],
        },
    ]
    t0 = time.perf_counter()
    try:
        completion = provider._client.chat.completions.parse(
            model=provider.model,
            messages=messages,
            response_format=GeoGuess,
            **provider.generation_kwargs(None),
        )
    except (ValidationError, LengthFinishReasonError, ContentFilterFinishReasonError, ValueError) as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        completion = getattr(exc, "completion", None)
        tin, tout = provider._usage(completion)
        raw = completion.choices[0].message.content or "" if completion is not None and completion.choices else ""
        return LLMResult(None, raw, tin, tout, elapsed, provider.name, f"{type(exc).__name__}: {exc}")
    latency = (time.perf_counter() - t0) * 1000
    msg = completion.choices[0].message
    tin, tout = provider._usage(completion)
    return LLMResult(
        parsed=msg.parsed,
        raw_text=msg.content or "",
        input_tokens=tin,
        output_tokens=tout,
        latency_ms=latency,
        model=completion.model or provider.name,
        error=f"rechazo: {msg.refusal}" if getattr(msg, "refusal", None) else None,
    )


def parse_json_guess(text: str) -> tuple[GeoGuess | None, str | None]:
    """Busca el objeto JSON de la respuesta (sin bloques <think> ni ```json) y lo valida."""
    from ..llm.foundry import strip_reasoning

    clean = strip_reasoning(text).replace("```json", "").replace("```", "")
    m = _JSON_OBJECT.search(clean)
    if not m:
        return None, "sin objeto JSON en la respuesta"
    try:
        data = json.loads(m.group(0))
        if isinstance(data, dict):
            data = {k: data[k] for k in GeoGuess.model_fields if k in data}  # sin claves de más
        return GeoGuess.model_validate(data), None
    except (ValidationError, ValueError, TypeError) as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


def _locate_text(provider: Any, image: bytes, mime: str) -> LLMResult[GeoGuess]:
    """Modelos sin `response_format` con esquema: JSON pedido en las instrucciones y validado aquí."""
    data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    system, user = _messages_text()
    messages = [
        {"role": "system", "content": system + JSON_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    t0 = time.perf_counter()
    completion = provider._client.chat.completions.create(
        model=provider.model, messages=messages, **provider.generation_kwargs(None)
    )
    latency = (time.perf_counter() - t0) * 1000
    tin, tout = provider._usage(completion)
    choice = completion.choices[0]
    raw = choice.message.content or ""
    parsed, error = parse_json_guess(raw)
    if parsed is None and choice.finish_reason == "length":
        error = "max_tokens agotado antes de responder"
    return LLMResult(parsed, raw, tin, tout, latency, completion.model or provider.name, error)


def _locate_claude(provider: Any, image: bytes, mime: str) -> LLMResult[GeoGuess]:
    from anthropic import transform_schema

    system, user = _messages_text()
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": transform_schema(GeoGuess)}}
    if getattr(provider, "effort", None):
        output_config["effort"] = provider.effort
    req: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": provider.max_tokens,
        "system": system,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(image).decode()},
                    },
                    {"type": "text", "text": user},
                ],
            }
        ],
        "output_config": output_config,
    }
    if getattr(provider, "thinking", None):
        req["thinking"] = {"type": provider.thinking}
    t0 = time.perf_counter()
    raw = provider._client.messages.with_raw_response.create(**req)
    latency = (time.perf_counter() - t0) * 1000
    body = raw.http_response.json()
    text = provider._text(body)
    parsed, error = None, None
    if text.strip():
        try:
            parsed = GeoGuess.model_validate_json(text)
        except (ValidationError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
    elif body.get("stop_reason") != "max_tokens":
        error = "respuesta sin bloque de texto"
    return provider._result(body, latency, parsed, error)


# Lugares de los que tira el mock: no sabe nada de la foto, solo usa un hash de los bytes.
_MOCK_PLACES = (
    ("España", "Castilla y León", 41.65, -4.72),
    ("Francia", "Auvernia", 45.77, 3.08),
    ("Noruega", "Vestland", 60.39, 5.32),
    ("Marruecos", "Marrakech-Safi", 31.63, -8.0),
    ("Kenia", "Rift Valley", -0.30, 36.07),
    ("India", "Himachal Pradesh", 32.10, 77.17),
    ("Japón", "Nagano", 36.65, 138.18),
    ("Australia", "Australia Occidental", -31.95, 115.86),
    ("Canadá", "Columbia Británica", 50.12, -122.95),
    ("Estados Unidos", "Colorado", 39.55, -105.78),
    ("México", "Chiapas", 16.75, -93.12),
    ("Perú", "Áncash", -9.53, -77.53),
    ("Argentina", "Mendoza", -32.89, -68.83),
    ("Brasil", "Minas Gerais", -19.92, -43.94),
    ("Nueva Zelanda", "Canterbury", -43.53, 172.64),
    ("China", "Yunnan", 25.04, 102.71),
)


class MockGeoProvider:
    """Rival de pruebas: determinista por imagen, sin red y sin acceso a la solución."""

    kind = "mock-geo"

    def __init__(self, name: str = "mock-geo", price_input_per_m: float = 0.0, price_output_per_m: float = 0.0):
        self.name = name
        self.price_input_per_m = price_input_per_m
        self.price_output_per_m = price_output_per_m

    def locate(self, image: bytes, mime: str = "image/jpeg") -> LLMResult[GeoGuess]:
        t0 = time.perf_counter()
        h = hashlib.sha256(image).digest()
        pais, region, lat, lon = _MOCK_PLACES[h[0] % len(_MOCK_PLACES)]
        lat += (h[1] - 128) / 64.0
        lon += (h[2] - 128) / 64.0
        guess = GeoGuess(
            pais=pais,
            region=region,
            lat=max(-90.0, min(90.0, lat)),
            lon=lon,
            confianza=round(0.2 + h[3] / 512.0, 2),
            razonamiento_breve="Soy el rival de pruebas: no miro la foto, elijo un sitio a partir de sus bytes.",
        )
        out = guess.model_dump_json()
        return LLMResult(guess, out, 800, len(out) // 4, (time.perf_counter() - t0) * 1000, self.name)
