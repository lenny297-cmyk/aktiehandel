"""Konfiguration och filsökvägar."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .fees import FeeModel
from .trading_calendar import TradingCalendar


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def docs_data(self) -> Path:
        return self.root / "docs" / "data"

    @property
    def config(self) -> Path:
        return self.root / "config.yaml"

    @property
    def universe(self) -> Path:
        return self.data / "universe.json"

    @property
    def meta(self) -> Path:
        return self.data / "meta.json"

    @property
    def portfolio(self) -> Path:
        return self.data / "portfolio.json"

    @property
    def trades(self) -> Path:
        return self.data / "trades.json"

    @property
    def daily_values(self) -> Path:
        return self.data / "daily_values.json"

    @property
    def decisions(self) -> Path:
        return self.data / "decisions.json"

    @property
    def runs(self) -> Path:
        return self.data / "runs.json"

    @property
    def summary(self) -> Path:
        return self.data / "summary.json"

    def published(self) -> list:
        """Filerna som kopieras till /docs och läses av sidan."""
        return [
            self.summary,
            self.portfolio,
            self.trades,
            self.daily_values,
            self.decisions,
            self.meta,
        ]


@dataclass(frozen=True)
class Config:
    raw: dict
    paths: Paths

    # ---------- laddning ----------

    @classmethod
    def load(cls, root=None) -> "Config":
        root_path = Path(root) if root else Path(__file__).resolve().parents[2]
        paths = Paths(root_path)
        with open(paths.config, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        return cls(raw=raw, paths=paths)

    # ---------- härledda objekt ----------

    @property
    def fees(self) -> FeeModel:
        return FeeModel.from_config(self.raw.get("fees", {}))

    @property
    def calendar(self) -> TradingCalendar:
        return TradingCalendar.from_config(self.raw)

    @property
    def start_capital(self) -> float:
        return float(self.raw.get("start_capital_sek", 100_000))

    @property
    def benchmark_ticker(self) -> str:
        return str(self.raw.get("benchmark_ticker", "^OMX"))

    @property
    def max_position_pct(self) -> float:
        return float(self.raw.get("risk", {}).get("max_position_pct", 0.20))

    @property
    def min_trade_sek(self) -> float:
        return float(self.raw.get("risk", {}).get("min_trade_sek", 1000))

    @property
    def max_proposals(self) -> int:
        return int(self.raw.get("risk", {}).get("max_proposals_per_run", 3))

    @property
    def history_days(self) -> int:
        return int(self.raw.get("data", {}).get("history_days", 60))

    @property
    def max_staleness_days(self) -> int:
        return int(self.raw.get("data", {}).get("max_staleness_days", 3))

    @property
    def model(self) -> dict:
        return dict(self.raw.get("model", {}))

    # ---------- universum ----------

    def universe(self) -> list:
        with open(self.paths.universe, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [entry["ticker"] for entry in data.get("constituents", [])]

    def universe_names(self) -> dict:
        with open(self.paths.universe, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {e["ticker"]: e.get("name", e["ticker"]) for e in data.get("constituents", [])}


def api_key() -> str:
    """Nyckeln läses bara från miljön och skrivs aldrig någonstans."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY saknas. Satt som GitHub Secret i produktion."
        )
    return key
