"""Esquemas pydantic de las salidas estructuradas del LLM y catálogo de patrones."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Patrones que el motor sabe detectar de forma determinista. El LLM solo puede
# elegir entre estos identificadores (o "otro"): así no inventa categorías.
PATTERNS: dict[str, str] = {
    "pieza_colgada": "dejar piezas sin defender",
    "mate_permitido": "no ver la amenaza de mate del rival",
    "mate_no_visto": "no rematar un mate que tenías",
    "rey_en_el_centro": "dejar el rey en el centro",
    "enroque_tardio": "no enrocar a tiempo",
    "cambio_desfavorable": "hacer cambios que pierden material",
    "oportunidad_perdida": "dejar pasar una ganancia clara",
    "dama_prematura": "sacar la dama demasiado pronto",
    "error_posicional": "jugadas que empeoran la posición sin perder material directo",
    "otro": "otros errores",
}

PatternId = Literal[
    "pieza_colgada",
    "mate_permitido",
    "mate_no_visto",
    "rey_en_el_centro",
    "enroque_tardio",
    "cambio_desfavorable",
    "oportunidad_perdida",
    "dama_prematura",
    "error_posicional",
    "otro",
]


class MoveProposal(BaseModel):
    """Salida estructurada de una jugada: un JSON con un único campo `move`."""

    move: str = Field(description="Una sola jugada en notación SAN (p. ej. Nf3, exd5, O-O, e8=Q)")


class ErrorExplicado(BaseModel):
    ply: int = Field(description="El campo 'ply' del hecho del motor que explicas")
    explicacion: str = Field(description="Una sola frase en español que explica el error")
    patron: PatternId


class PatronRecurrente(BaseModel):
    patron: PatternId
    descripcion: str = Field(description="Una frase sobre cómo se repite en sus partidas")


class Diagnostico(BaseModel):
    titular: str = Field(
        description="Frase corta con la causa principal, coherente con el resultado del jugador y basada en los hechos"
    )
    error_principal_ply: int = Field(description="ply del error más grave según los hechos del motor")
    errores: list[ErrorExplicado]
    patrones_recurrentes: list[PatronRecurrente]
    consejo: str = Field(description="Un consejo práctico de una frase para la próxima partida")


class Pista(BaseModel):
    """Salida estructurada de una pista: la explicación de por qué la jugada del motor es la mejor."""

    explicacion: str = Field(description="2 o 3 frases en español de España, solo con los motivos y la línea del motor")
