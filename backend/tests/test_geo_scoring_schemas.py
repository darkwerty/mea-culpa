import math

import pytest
from pydantic import ValidationError

from meaculpa.geo.schemas import GeoGuess, GuessIn
from meaculpa.geo.scoring import haversine_km, score


def test_haversine_known_distances():
    assert haversine_km(40.4168, -3.7038, 40.4168, -3.7038) == 0
    # Madrid - Barcelona ~505 km
    assert haversine_km(40.4168, -3.7038, 41.3874, 2.1686) == pytest.approx(505, abs=5)
    # Antípodas: media circunferencia
    assert haversine_km(0, 0, 0, 180) == pytest.approx(math.pi * 6371.0088, rel=1e-6)
    # Cruzar el antimeridiano es corto
    assert haversine_km(0, 179.5, 0, -179.5) == pytest.approx(111.2, abs=0.5)


def test_score_curve():
    assert score(0) == 5000
    assert score(2000) == round(5000 / math.e)
    assert score(20000) < 5
    assert score(100) > score(1000) > score(5000)
    with pytest.raises(ValueError):
        score(-1)


def _guess(**kw):
    base = {
        "pais": "Perú",
        "region": "Cusco",
        "lat": -13.2,
        "lon": -72.5,
        "confianza": 0.7,
        "razonamiento_breve": "Andes",
    }
    return GeoGuess(**{**base, **kw})


def test_guess_valid_and_cleaned():
    g = _guess(razonamiento_breve="Terrazas incas · montañas — clima seco – altura", confianza=85)
    assert g.confianza == 0.85
    assert "·" not in g.razonamiento_breve and "—" not in g.razonamiento_breve
    assert "–" not in g.razonamiento_breve


@pytest.mark.parametrize(
    "bad",
    [{"lat": 91}, {"lon": -181}, {"confianza": -0.1}, {"confianza": 150}, {"pais": "  "}],
)
def test_guess_out_of_range_rejected(bad):
    with pytest.raises(ValidationError):
        _guess(**bad)


def test_guess_extra_fields_rejected():
    with pytest.raises(ValidationError):
        GeoGuess.model_validate_json(
            '{"pais":"x","region":"y","lat":1,"lon":2,"confianza":0.5,"razonamiento_breve":"z","extra":1}'
        )


def test_guess_from_json_missing_field():
    with pytest.raises(ValidationError):
        GeoGuess.model_validate_json('{"pais":"x","lat":1,"lon":2}')


def test_user_guess_wraps_longitude():
    assert GuessIn(lat=10, lon=190).lon == -170
    assert GuessIn(lat=10, lon=-540).lon == -180
    with pytest.raises(ValidationError):
        GuessIn(lat=95, lon=0)
