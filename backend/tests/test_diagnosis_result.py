"""El diagnóstico cuadra con el resultado: nunca «has perdido» en una victoria o en tablas, ni «Otra vez» sin historial."""

from __future__ import annotations

from functools import lru_cache

import chess
from conftest import PGN_DIR

from meaculpa.analysis import (
    AlphaBetaAnalyzer,
    analyze_moves,
    coherence_problems,
    contradictions,
    diagnose,
    fallback_headline,
    history_counts,
    parse_pgn,
    pgn_player_result,
    player_result,
)
from meaculpa.llm import MockProvider
from meaculpa.llm.base import LLMResult
from meaculpa.prompts import DIAG_SYSTEM, diagnosis_messages
from meaculpa.schemas import Diagnostico, ErrorExplicado

BUG_HEADLINE = "Otra vez has perdido por dejar el rey en el centro y hacer un cambio desfavorable"


class ScriptedDiagnosis:
    """Devuelve los titulares indicados, uno por llamada (el último se repite). Guarda los mensajes recibidos."""

    name, kind = "guion", "test"

    def __init__(self, headlines: list[str], consejo: str = "Mira las amenazas antes de mover.") -> None:
        self.headlines = list(headlines)
        self.consejo = consejo
        self.calls: list[list[dict]] = []

    def structured(self, messages, schema, temperature=None):
        self.calls.append(messages)
        user = messages[1]["content"]
        import json

        from meaculpa.prompts import extract_tag

        facts = json.loads(extract_tag(user, "hechos_motor") or "[]")
        headline = self.headlines.pop(0) if len(self.headlines) > 1 else self.headlines[0]
        parsed = Diagnostico(
            titular=headline,
            error_principal_ply=max(facts, key=lambda f: f["caida_cp"])["ply"] if facts else 0,
            errores=[
                ErrorExplicado(ply=f["ply"], explicacion="Esa jugada empeoró la posición.", patron="otro")
                for f in facts
            ],
            patrones_recurrentes=[],
            consejo=self.consejo,
        )
        return LLMResult(parsed, parsed.model_dump_json(), 100, 50, 1.0, self.name)


def _analysis(_pgn_text=None):
    return _cached_analysis()


@lru_cache(maxsize=1)
def _cached_analysis():
    """Análisis de legal_1750.pgn (Bxd1 permite mate): se calcula una vez para todo el módulo."""
    game = parse_pgn((PGN_DIR / "legal_1750.pgn").read_text(encoding="utf-8"))
    color = chess.WHITE if game.headers["MeaCulpaUser"] == "white" else chess.BLACK
    return analyze_moves(list(game.mainline_moves()), color, AlphaBetaAnalyzer(3))


def test_prompt_states_player_result_and_has_examples_for_each_case(pgn_text):
    msgs = diagnosis_messages("1. e4 e5", "victoria", "blancas", [], {}, [])
    assert "<resultado_jugador>victoria</resultado_jugador>" in msgs[1]["content"]
    assert "ha GANADO" in msgs[1]["content"]
    assert "<patrones_repetidos>[]</patrones_repetidos>" in msgs[1]["content"]
    assert "Has ganado, pero casi la regalas" in DIAG_SYSTEM
    assert "Tablas que podían haber sido victoria" in DIAG_SYSTEM
    assert "Has perdido por" in DIAG_SYSTEM
    # El único ejemplo con «Otra vez» va atado a que el patrón esté repetido.
    assert DIAG_SYSTEM.count("Otra vez has perdido") == 1 and "patrón repetido" in DIAG_SYSTEM
    for ch in "·—–":
        assert ch not in DIAG_SYSTEM


def test_player_result_helpers():
    assert player_result("victoria") == "victoria" and player_result(None) == "en curso"
    assert pgn_player_result("1-0", chess.WHITE) == "victoria"
    assert pgn_player_result("1-0", chess.BLACK) == "derrota"
    assert pgn_player_result("0-1", chess.BLACK) == "victoria"
    assert pgn_player_result("1/2-1/2", chess.WHITE) == "tablas"
    assert pgn_player_result("*", chess.WHITE) == "desconocido"


def test_contradiction_detector():
    assert contradictions(BUG_HEADLINE, "victoria") == ["has perdido"]
    assert contradictions(BUG_HEADLINE, "tablas") == ["has perdido"]
    assert contradictions(BUG_HEADLINE, "derrota") == []
    assert contradictions("Has ganado, aunque has perdido la dama por el camino", "victoria") == []
    assert contradictions("Tablas que podían haber sido victoria", "tablas") == []
    assert contradictions("Has ganado por los pelos", "derrota") == ["has ganado"]
    assert contradictions("Has ganado por los pelos", "tablas") == ["has ganado"]
    assert contradictions("Perdiste material en la jugada 12", "victoria") == []
    assert contradictions("Has perdido por no enrocar", "en curso") == ["has perdido"]
    assert contradictions("La partida acabó en tablas", "victoria") == ["has hecho tablas"]


def test_bug_case_is_fixed_by_a_retry(pgn_text, settings):
    """El caso de Daniel: ganó y el titular decía «Otra vez has perdido...». El reintento lo corrige."""
    analysis = _analysis(pgn_text)
    provider = ScriptedDiagnosis([BUG_HEADLINE, "Has ganado, pero casi la regalas al permitir un mate"])
    history = history_counts([], analysis.patrones_partida)
    result = diagnose(analysis, provider, "1. e4", "victoria", history, settings=settings)
    assert result.diagnostico["titular"] == "Has ganado, pero casi la regalas al permitir un mate"
    assert result.grounding["coherencia"] == "reintento" and result.llamadas == 2
    assert result.input_tokens == 200  # se suman los tokens de las dos llamadas
    feedback = provider.calls[1][-1]["content"]
    assert "has perdido" in feedback and "victoria" in feedback
    assert provider.calls[1][-2]["role"] == "assistant"


def test_bug_case_falls_back_without_llm_in_a_win(pgn_text, settings):
    analysis = _analysis(pgn_text)
    provider = ScriptedDiagnosis([BUG_HEADLINE], consejo="La próxima vez que hayas perdido, repasa la apertura.")
    result = diagnose(
        analysis, provider, "1. e4", "victoria", history_counts([], analysis.patrones_partida), settings=settings
    )
    diag = result.diagnostico
    assert result.grounding["coherencia"] == "respaldo" and result.llamadas == 2
    assert diag["titular"].startswith("Has ganado")
    assert "perdido" not in diag["titular"] and "Otra vez" not in diag["titular"]
    assert coherence_problems(diag, "victoria", again_allowed=False) == []


def test_draw_never_says_lost_or_won(pgn_text, settings):
    analysis = _analysis(pgn_text)
    provider = ScriptedDiagnosis(["Has perdido por permitir un mate", "Has ganado por los pelos"])
    result = diagnose(
        analysis, provider, "1. e4", "tablas", history_counts([], analysis.patrones_partida), settings=settings
    )
    titular = result.diagnostico["titular"]
    assert titular.startswith("Tablas") and contradictions(titular, "tablas") == []
    assert result.grounding["coherencia"] == "respaldo"


def test_loss_keeps_a_coherent_headline_and_drops_unbacked_otra_vez(pgn_text, settings):
    analysis = _analysis(pgn_text)
    history = history_counts([], analysis.patrones_partida)  # sin partidas anteriores
    ok = diagnose(
        analysis,
        ScriptedDiagnosis(["Has perdido por permitir un mate"]),
        "1. e4",
        "derrota",
        history,
        settings=settings,
    )
    assert ok.grounding["coherencia"] == "ok" and ok.llamadas == 1
    again = diagnose(
        analysis,
        ScriptedDiagnosis(["Otra vez has perdido por permitir un mate"]),
        "1. e4",
        "derrota",
        history,
        settings=settings,
    )
    assert again.diagnostico["titular"] == "Has perdido por no ver la amenaza de mate del rival"
    won = diagnose(
        analysis, ScriptedDiagnosis(["Has ganado con estilo"]), "1. e4", "derrota", history, settings=settings
    )
    assert won.diagnostico["titular"].startswith("Has perdido")


def test_otra_vez_is_kept_when_the_pattern_really_repeats(pgn_text, settings):
    analysis = _analysis(pgn_text)
    history = history_counts([["mate_permitido"]], analysis.patrones_partida)
    result = diagnose(
        analysis,
        ScriptedDiagnosis(["Otra vez has perdido por permitir un mate"]),
        "1. e4",
        "derrota",
        history,
        settings=settings,
    )
    assert result.grounding["coherencia"] == "ok"
    assert result.diagnostico["titular"].startswith("Otra vez")
    # Un patrón repetido que NO es el de la causa principal no da derecho a «Otra vez».
    other = history_counts([["dama_prematura"], ["dama_prematura"]], analysis.patrones_partida)
    res2 = diagnose(
        analysis,
        ScriptedDiagnosis(["Otra vez has perdido por permitir un mate"]),
        "1. e4",
        "derrota",
        other,
        settings=settings,
    )
    assert not res2.diagnostico["titular"].startswith("Otra vez")


def test_mock_headlines_match_each_result(pgn_text, settings):
    analysis = _analysis(pgn_text)
    history = history_counts([], analysis.patrones_partida)
    titles = {
        r: diagnose(analysis, MockProvider(), "1. e4", r, history, settings=settings).diagnostico["titular"]
        for r in ("victoria", "tablas", "derrota", "en curso")
    }
    assert titles["victoria"].startswith("Has ganado")
    assert titles["tablas"].startswith("Tablas")
    assert titles["derrota"].startswith("Has perdido")
    assert "perdido" not in titles["en curso"] and "ganado" not in titles["en curso"]


def test_fallback_headlines_are_coherent():
    for result in ("victoria", "tablas", "derrota", "en curso", "desconocido"):
        for pattern in (None, "rey_en_el_centro"):
            for again in (False, True):
                assert contradictions(fallback_headline(result, pattern, again), result) == []
    assert (
        fallback_headline("derrota", "rey_en_el_centro", True) == "Otra vez has perdido por dejar el rey en el centro"
    )


def test_contradiction_detector_catches_other_ways_of_saying_it():
    lost = [
        "Otra vez pierdes por dejar el rey en el centro",
        "Pierdes la partida por no enrocar",
        "Derrota por dejar el rey en el centro",
        "Te ha ganado por no enrocar a tiempo",
        "Te ganó la IA por dejar piezas sin defender",
        "Has acabado perdiendo por un cambio desfavorable",
        "Acabaste perdiendo con el rey en el centro",
        "Partida perdida por un cambio desfavorable",
        "Fuiste derrotado por no enrocar",
        "Mal día: derrota con el rey en el centro",
    ]
    for text in lost:
        assert contradictions(text, "victoria") == ["has perdido"], text
        assert contradictions(text, "derrota") == [], text
    won = ["Ganas la partida pese a todo", "Victoria pese a dejar el rey en el centro", "Acabas ganando con sustos"]
    for text in won:
        assert contradictions(text, "derrota") == ["has ganado"], text
        assert contradictions(text, "victoria") == [], text
    assert contradictions("Empatas por no rematar", "victoria") == ["has hecho tablas"]
    # Frases que no hablan del resultado.
    innocent = [
        "Has ganado, aunque pierdes la iniciativa en la jugada 14",
        "Con ganas de atacar, dejaste el rey en el centro",
        "Pierdes material si no miras las amenazas",
        "Ganas tiempo si desarrollas antes de sacar la dama",
        "Te ha ganado la calidad con un truco táctico",
        "Has ganado, pero casi la regalas por dejar el rey en el centro",
    ]
    for text in innocent:
        assert contradictions(text, "victoria") == [], text


def test_otra_vez_at_the_start_of_the_advice_needs_history():
    diag = {"titular": "Has ganado, pero casi la regalas", "consejo": "Otra vez: enroca antes de atacar", "errores": []}
    assert coherence_problems(diag, "victoria", again_allowed=False)
    assert coherence_problems(diag, "victoria", again_allowed=True) == []
    diag["consejo"] = "Enroca pronto para que no te pase otra vez"
    assert coherence_problems(diag, "victoria", again_allowed=False) == []


def test_resignation_reaches_the_prompt_and_the_fallback(pgn_text, settings):
    analysis = _analysis(pgn_text)
    history = history_counts([], analysis.patrones_partida)
    messages = diagnosis_messages("1. e4", "derrota", "blancas", [], history, [], "abandono")
    assert "<fin>abandono</fin>" in messages[1]["content"] and "SE RINDIÓ" in messages[1]["content"]
    assert "<fin>" not in diagnosis_messages("1. e4", "en curso", "blancas", [], history, [], "abandono")[1]["content"]
    mate = diagnosis_messages("1. e4", "derrota", "blancas", [], history, [], "jaque mate")[1]["content"]
    assert "<fin>jaque mate</fin>" in mate and "SE RINDIÓ" not in mate
    assert fallback_headline("derrota", None, False, "abandono").startswith("Te rendiste")
    assert fallback_headline("derrota", "pieza_colgada", False, "abandono") == (
        "Te rendiste, y tu error más caro fue dejar piezas sin defender"
    )
    # Con el titular contradictorio en una derrota por abandono, el respaldo habla de la rendición.
    provider = ScriptedDiagnosis(["Has ganado de milagro"])
    result = diagnose(analysis, provider, "1. e4", "derrota", history, settings=settings, end_reason="abandono")
    assert result.grounding["coherencia"] == "respaldo" and result.grounding["fin"] == "abandono"
    assert result.diagnostico["titular"].startswith("Te rendiste")
    assert "<fin>abandono</fin>" in provider.calls[0][1]["content"]
