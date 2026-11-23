"""Bygger underlaget sidan läser och kopierar det till /docs.

Sidan ska inte räkna om någonting. Allt den visar räknas ut här och läggs i
summary.json, så att webbsidan bara presenterar färdiga siffror.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from .benchmark import performance
from .fees import round_money, round_price

log = logging.getLogger(__name__)

DISCLAIMER = (
    "Simulerat experiment. Inga riktiga order läggs och inget riktigt kapital "
    "förvaltas. Detta är inte finansiell rådgivning."
)


def build_summary(
    *,
    generated_at: str,
    slot: Optional[str],
    meta: dict,
    portfolio,
    prices: dict,
    daily_values: Sequence,
    trades: Sequence,
    decision: Optional[dict],
    names: dict,
    index_level: float,
    index_value: float,
    skipped: Optional[str] = None,
) -> dict:
    start_capital = float(meta.get("start_capital", 100_000))
    perf = performance(list(daily_values), start_capital)

    positions = []
    for pos in portfolio.sorted_positions():
        price = prices.get(pos.ticker)
        entry = {
            "ticker": pos.ticker,
            "name": names.get(pos.ticker, pos.ticker),
            "shares": pos.shares,
            "avg_price": pos.avg_price,
            "cost_basis": round_money(pos.cost_basis),
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "opened_at": pos.opened_at,
            "rationale": pos.rationale,
        }
        if price is not None:
            entry["last_price"] = round_price(price)
            entry["market_value"] = pos.market_value(price)
            entry["unrealized_pnl"] = pos.unrealized_pnl(price)
            entry["unrealized_pct"] = (
                round((pos.market_value(price) / pos.cost_basis - 1.0) * 100.0, 2)
                if pos.cost_basis
                else None
            )
            total = portfolio.total_value(prices) if prices else 0.0
            entry["weight_pct"] = (
                round(pos.market_value(price) / total * 100.0, 2) if total else None
            )
        positions.append(entry)

    closed = [t for t in trades if t.get("action") == "sell"]
    closed = list(reversed(closed))[:10]

    series = [
        {
            "date": entry["date"],
            "portfolio": entry["portfolio_value"],
            "index": entry["index_value"],
        }
        for entry in daily_values
    ]

    return {
        "generated_at": generated_at,
        "slot": slot,
        "skipped": skipped,
        "disclaimer": DISCLAIMER,
        "start": {
            "date": meta.get("start_date"),
            "capital": round_money(start_capital),
            "index_level": meta.get("index_start_level"),
        },
        "benchmark": {
            "ticker": meta.get("benchmark_ticker", "^OMX"),
            "level": round_price(index_level) if index_level else None,
            "value_sek": round_money(index_value) if index_value else None,
        },
        "performance": perf,
        "cash": round_money(portfolio.cash),
        "market_value": portfolio.market_value(prices) if prices else 0.0,
        "total_value": portfolio.total_value(prices) if prices else round_money(portfolio.cash),
        "realized_pnl": round_money(portfolio.realized_pnl),
        "positions": positions,
        "recent_closed": closed,
        "last_decision": decision,
        "series": series,
    }


def publish(store, paths) -> None:
    """Kopiera state-filerna till /docs så att sidan kommer åt dem."""
    for source in paths.published():
        destination = paths.docs_data / source.name
        store.copy(source, destination)
    log.info("publicerade %d filer till %s", len(paths.published()), paths.docs_data)
