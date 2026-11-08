"""Salida estructurada que se pide al modelo y su validación.

El esquema JSON que recibe el modelo no lleva `minimum`/`maximum` (no todos los modos estrictos los aceptan):
los rangos se comprueban aquí, al validar, y una respuesta fuera de rango cuenta como respuesta no válida.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

# Caracteres que no se muestran en la web (reglas de estilo de danielalonso.dev).
_FORBIDDEN = str.maketrans({"·": ",", "—": ",", "–": "-", "•": ","})
_SPACES = re.compile(r"\s+")
MAX_CLUES = 4
_BULLET = re.compile(r"^(?:[-*]|\d+[.)])\s*")  # viñetas o numeración que algunos modelos añaden
MAX_CLUE = 140  # caracteres por pista


def clean_text(value: str, limit: int) -> str:
    text = _SPACES.sub(" ", str(value).translate(_FORBIDDEN)).strip()
    return text[:limit].rstrip()


class GeoGuess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pais: str
    region: str
    lat: float
    lon: float
    confianza: float
    razonamiento_breve: str
    # De 2 a 4 pistas visibles en la imagen y cómo llevan a la respuesta (o una sola si no hay pistas claras).
    pistas: list[str]

    @field_validator("lat")
    @classmethod
    def _lat(cls, v: float) -> float:
        if not -90.0 <= v <= 90.0:
            raise ValueError("lat fuera de [-90, 90]")
        return v

    @field_validator("lon")
    @classmethod
    def _lon(cls, v: float) -> float:
        if not -180.0 <= v <= 180.0:
            raise ValueError("lon fuera de [-180, 180]")
        return v

    @field_validator("confianza")
    @classmethod
    def _conf(cls, v: float) -> float:
        # Algunos modelos responden en porcentaje: 0-1 se acepta tal cual, 1-100 se reescala.
        if 1.0 < v <= 100.0:
            v = v / 100.0
        if not 0.0 <= v <= 1.0:
            raise ValueError("confianza fuera de [0, 1]")
        return round(v, 3)

    @field_validator("pais", "region")
    @classmethod
    def _short(cls, v: str) -> str:
        v = clean_text(v, 80)
        if not v:
            raise ValueError("vacío")
        return v

    @field_validator("razonamiento_breve")
    @classmethod
    def _reason(cls, v: str) -> str:
        return clean_text(v, 400)

    @field_validator("pistas")
    @classmethod
    def _clues(cls, v: list[str]) -> list[str]:
        """Como mucho 4, cortas y sin los caracteres que la web no muestra. Vacía si el modelo no da ninguna."""
        out = [_BULLET.sub("", clean_text(x, MAX_CLUE)) for x in v if isinstance(x, str)]
        return [x for x in out if x][:MAX_CLUES]


class GuessIn(BaseModel):
    """Marca del jugador en el mapa."""

    model_config = ConfigDict(extra="forbid")

    lat: float
    lon: float

    @field_validator("lat")
    @classmethod
    def _lat(cls, v: float) -> float:
        if not -90.0 <= v <= 90.0:
            raise ValueError("lat fuera de [-90, 90]")
        return round(v, 5)

    @field_validator("lon")
    @classmethod
    def _lon(cls, v: float) -> float:
        # Leaflet devuelve longitudes fuera de [-180, 180] si el mapa ha dado la vuelta al mundo.
        v = ((v + 180.0) % 360.0) - 180.0
        return round(v, 5)
