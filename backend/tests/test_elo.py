from __future__ import annotations

import math

import pytest

from meaculpa.elo import estimate, expected, fit_ratings


def test_expected_score_is_logistic():
    assert expected(1500, 1500) == pytest.approx(0.5)
    assert expected(1900, 1500) == pytest.approx(10 / 11)


def test_mle_against_one_anchor_matches_closed_form():
    games = [("x", "a", 1.0)] * 7 + [("x", "a", 0.5)] + [("x", "a", 0.0)] * 2  # 75 %
    r = fit_ratings(games, {"a": 1500})["x"]
    assert r == pytest.approx(1500 + 400 * math.log10(3), abs=0.01)


def test_mle_against_two_anchors_is_between_them():
    games = [("x", "lo", 1.0)] * 8 + [("x", "lo", 0.0)] * 2 + [("x", "hi", 1.0)] * 2 + [("x", "hi", 0.0)] * 8
    r = fit_ratings(games, {"lo": 1300, "hi": 1700})["x"]
    assert r == pytest.approx(1500, abs=0.5)  # simétrico


def test_joint_estimation_with_games_between_free_players():
    games = [("x", "a", 0.5)] * 20 + [("y", "x", 1.0)] * 15 + [("y", "x", 0.0)] * 5
    r = fit_ratings(games, {"a": 1500})
    assert r["x"] == pytest.approx(1500, abs=1)
    assert r["y"] == pytest.approx(1500 + 400 * math.log10(3), abs=1)


def test_perfect_score_is_not_published_and_ci_contains_estimate():
    est = estimate([("z", "a", 1.0)] * 6, {"a": 1500}, bootstrap=50)
    assert est["z"].rating is None
    games = [("x", "a", 1.0)] * 12 + [("x", "a", 0.5)] * 6 + [("x", "a", 0.0)] * 12
    e = estimate(games, {"a": 1600}, bootstrap=300, seed=1)["x"]
    assert e.rating == pytest.approx(1600, abs=0.5)
    assert e.ci_low < e.rating < e.ci_high
    assert 50 < e.ci_high - e.ci_low < 400  # 30 partidas: IC de ±100-150 Elo aprox.
    assert e.games == 30 and e.score == 15
