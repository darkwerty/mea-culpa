from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from meaculpa.config import get_settings

PGN_DIR = Path(__file__).resolve().parents[2] / "data" / "pgn"


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(get_settings(), data_dir=tmp_path, analysis_depth=3, llm_provider="mock")


@pytest.fixture
def pgn_text():
    def _read(name: str) -> str:
        return (PGN_DIR / name).read_text(encoding="utf-8")

    return _read
