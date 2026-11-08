"""Proveedor simulado: funciona sin Azure y es el que usan los tests.

Lee exactamente los mismos prompts que vería el modelo real (FEN, hechos del motor) y
responde con el mismo esquema pydantic. Para jugar, propone a veces jugadas ilegales
(más cuanto mayor es la temperatura) para ejercitar el bucle de reintentos. Para el
diagnóstico, rellena plantillas en español con los hechos: no "entiende" nada, pero
permite probar todo el flujo de punta a punta.
"""

from __future__ import annotations

import json
import random
import time

import chess

from ..prompts import extract_tag
from ..schemas import PATTERNS, Diagnostico, ErrorExplicado, MoveProposal, PatronRecurrente, Pista
from .base import LLMResult, T

_TEMPLATES = {
    "pieza_colgada": "Con {jugada} dejaste {detalle} sin defensa y la evaluación cayó de {antes} a {despues}; {mejor} lo evitaba.",
    "mate_permitido": "{jugada} permitió un mate ({despues}); había que jugar {mejor}.",
    "mate_no_visto": "Tenías mate ({antes}) y con {jugada} lo dejaste escapar; el remate era {mejor}.",
    "rey_en_el_centro": "Con el rey aún en el centro, {jugada} abrió el juego en tu contra ({antes} → {despues}); mejor {mejor}.",
    "cambio_desfavorable": "El cambio con {jugada} te costó material ({antes} → {despues}); {mejor} era más sólido.",
    "oportunidad_perdida": "{jugada} dejó pasar {mejor}, que ganaba material ({antes} → {despues}).",
    "dama_prematura": "{jugada} sacó la dama demasiado pronto y perdió tiempos ({antes} → {despues}); mejor {mejor}.",
    "enroque_tardio": "Sin enrocar, {jugada} te dejó expuesto ({antes} → {despues}); {mejor} era lo indicado.",
    "error_posicional": "{jugada} empeoró tu posición de {antes} a {despues}; el motor prefería {mejor}.",
}


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockProvider:
    kind = "mock"

    supports_structured = True
    # Temperatura que «usa» el mock cuando no se le pasa ninguna (como el valor por defecto de un proveedor).
    DEFAULT_TEMPERATURE = 0.7

    def __init__(
        self,
        name: str = "mock-llm",
        seed: int | None = 7,
        illegal_base: float = 0.05,
        price_input_per_m: float = 0.0,
        price_output_per_m: float = 0.0,
    ) -> None:
        self.name = name
        self._rng = random.Random(seed)
        self.illegal_base = illegal_base
        self.price_input_per_m = price_input_per_m
        self.price_output_per_m = price_output_per_m

    def structured(
        self, messages: list[dict[str, str]], schema: type[T], temperature: float | None = None
    ) -> LLMResult[T]:
        if temperature is None:
            temperature = self.DEFAULT_TEMPERATURE
        t0 = time.perf_counter()
        if schema is MoveProposal:
            parsed: object = self._move(messages, temperature)
        elif schema is Diagnostico:
            parsed = self._diagnosis(messages)
        elif schema is Pista:
            parsed = self._hint(messages)
        else:  # pragma: no cover - uso indebido
            raise TypeError(f"MockProvider no sabe responder con {schema.__name__}")
        prompt_text = "".join(m["content"] for m in messages)
        out = parsed.model_dump_json()  # type: ignore[attr-defined]
        return LLMResult(
            parsed=parsed,  # type: ignore[arg-type]
            raw_text=out,
            input_tokens=_estimate_tokens(prompt_text),
            output_tokens=_estimate_tokens(out),
            latency_ms=(time.perf_counter() - t0) * 1000,
            model=self.name,
        )

    def text(self, messages: list[dict[str, str]], temperature: float | None = None) -> LLMResult[None]:
        """Modo texto: la misma jugada que en modo estructurado, dentro de una frase."""
        res = self.structured(messages, MoveProposal, temperature)
        out = f"Creo que lo mejor es jugar {res.parsed.move}."  # type: ignore[union-attr]
        return LLMResult(None, out, res.input_tokens, _estimate_tokens(out), res.latency_ms, self.name)

    # ------------------------------------------------------------------ jugar
    def _move(self, messages: list[dict[str, str]], temperature: float) -> MoveProposal:
        fen = extract_tag(messages[1]["content"], "fen")
        board = chess.Board(fen) if fen else chess.Board()
        retries = sum(1 for m in messages[2:] if m["role"] == "user")
        # Cuanta más temperatura, más jugadas ilegales; tras cada error "aprende" un poco.
        p_illegal = (self.illegal_base + 0.45 * temperature) / (1 + retries)
        legal = list(board.legal_moves)
        if self._rng.random() < p_illegal:
            return MoveProposal(move=self._illegal_san(board))
        captures = [m for m in legal if board.is_capture(m)]
        pick = self._rng.choice(captures) if captures and self._rng.random() < 0.6 else self._rng.choice(legal)
        return MoveProposal(move=board.san(pick))

    def _illegal_san(self, board: chess.Board) -> str:
        options = ["Ke9", "Qxh9", "O-O-O-O", "e5e4e3"]
        piece_letters = "NBRQK"
        letter = self._rng.choice(piece_letters)
        square = chess.square_name(self._rng.randrange(64))
        candidate = f"{letter}{square}"
        try:
            board.parse_san(candidate)
        except ValueError:
            return candidate
        return self._rng.choice(options)

    # ----------------------------------------------------------- diagnóstico
    def _diagnosis(self, messages: list[dict[str, str]]) -> Diagnostico:
        user = messages[1]["content"]
        facts = json.loads(extract_tag(user, "hechos_motor") or "[]")
        history = json.loads(extract_tag(user, "historial_patrones") or "{}")
        errores: list[ErrorExplicado] = []
        for f in facts:
            tags = f.get("etiquetas") or ["error_posicional"]
            patron = tags[0] if tags[0] in _TEMPLATES else "error_posicional"
            texto = _TEMPLATES[patron].format(
                jugada=f["jugada"],
                antes=f["eval_antes"],
                despues=f["eval_despues"],
                mejor=f.get("mejor_jugada") or "otra jugada",
                detalle=(f.get("detalle") or {}).get("pieza_en_peligro", "una pieza"),
            )
            errores.append(ErrorExplicado(ply=f["ply"], explicacion=texto, patron=patron))

        principal = max(facts, key=lambda f: f["caida_cp"]) if facts else None
        recurrentes = [
            PatronRecurrente(patron=p, descripcion=f"Te ha pasado en {n} de {history.get('partidas', 0)} partidas.")
            for p, n in sorted(history.get("recuentos", {}).items(), key=lambda kv: -kv[1])
            if n >= 2 and p in PATTERNS
        ]
        repeated = set(json.loads(extract_tag(user, "patrones_repetidos") or "[]"))
        if principal:
            main_pat = next((e.patron for e in errores if e.ply == principal["ply"]), "error_posicional")
            result = extract_tag(user, "resultado_jugador")
            again = main_pat in repeated
            cause = PATTERNS[main_pat]
            if result == "derrota":
                titular = f"{'Otra vez has' if again else 'Has'} perdido por {cause}."
            elif result == "victoria":
                titular = f"Has ganado, pero {'otra vez caíste en' if again else 'casi la regalas por'} {cause}."
            elif result == "tablas":
                titular = f"Tablas que podían haber sido más: {'otra vez' if again else 'te costó'} {cause}."
            else:
                titular = f"{'Otra vez el mismo fallo' if again else 'Tu error más caro'}: {cause}."
            consejo = f"Antes de mover, pregúntate qué amenaza el rival; en la jugada {principal['jugada_numero']} tocaba {principal.get('mejor_jugada')}."
        else:
            titular = "Partida limpia: el motor no encuentra errores graves tuyos."
            consejo = "Sigue así y prueba un rival más fuerte."
        return Diagnostico(
            titular=titular,
            error_principal_ply=principal["ply"] if principal else 0,
            errores=errores,
            patrones_recurrentes=recurrentes,
            consejo=consejo,
        )

    # ----------------------------------------------------------------- pistas
    def _hint(self, messages: list[dict[str, str]]) -> Pista:
        user = messages[1]["content"]
        best = extract_tag(user, "mejor_jugada") or "la jugada del motor"
        reasons = json.loads(extract_tag(user, "motivos") or "[]")
        body = " ".join(r for r in reasons[:2])
        return Pista(explicacion=f"{best} es la mejor jugada aquí. {body}".strip())


class ScriptedProvider:
    """Devuelve jugadas predefinidas en orden. Útil para tests de reintento/fallback."""

    kind = "scripted"

    def __init__(
        self,
        moves: list[str | None],
        name: str = "scripted",
        supports_structured: bool = True,
        price_input_per_m: float = 0.0,
        price_output_per_m: float = 0.0,
    ) -> None:
        self._moves = list(moves)
        self.name = name
        self.supports_structured = supports_structured
        self.price_input_per_m = price_input_per_m
        self.price_output_per_m = price_output_per_m
        self.calls: list[list[dict[str, str]]] = []
        self.temperatures: list[float | None] = []

    def structured(
        self, messages: list[dict[str, str]], schema: type[T], temperature: float | None = None
    ) -> LLMResult[T]:
        self.calls.append([dict(m) for m in messages])
        self.temperatures.append(temperature)
        nxt = self._moves.pop(0) if self._moves else None
        if nxt is None:  # simula salida que no cumple el esquema
            return LLMResult(None, "blah", 10, 2, 1.0, self.name, error="no cumple el esquema")
        parsed = MoveProposal(move=nxt)
        return LLMResult(parsed, parsed.model_dump_json(), 10, 5, 1.0, self.name)  # type: ignore[arg-type]

    def text(self, messages: list[dict[str, str]], temperature: float | None = None) -> LLMResult[None]:
        """En modo texto, cada elemento del guion es la respuesta completa del modelo."""
        self.calls.append([dict(m) for m in messages])
        self.temperatures.append(temperature)
        nxt = self._moves.pop(0) if self._moves else None
        return LLMResult(None, nxt or "", 10, 5, 1.0, self.name)
