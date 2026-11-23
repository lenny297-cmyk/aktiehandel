"""Gemensamma fixtures. Inga tester rör nätet eller skriver till data/."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiaktier.fees import FeeModel  # noqa: E402
from aiaktier.portfolio import Portfolio  # noqa: E402

TS = "2026-11-23T09:15:00+01:00"


@pytest.fixture
def fees() -> FeeModel:
    """0,25 % courtage med 1 kr minimum och 0,10 % total spread."""
    return FeeModel(commission_pct=0.0025, commission_min_sek=1.0, spread_pct=0.0010)


@pytest.fixture
def free_fees() -> FeeModel:
    """Utan avgifter. Används för att isolera ren bokföringsaritmetik."""
    return FeeModel(commission_pct=0.0, commission_min_sek=0.0, spread_pct=0.0)


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio.new(100_000)


@pytest.fixture
def universe() -> list:
    return ["ERIC-B.ST", "VOLV-B.ST", "ABB.ST", "AZN.ST"]
