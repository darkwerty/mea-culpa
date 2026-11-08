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
