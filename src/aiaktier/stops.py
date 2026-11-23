"""Kontroll av stopp och mål.

Körs före varje beslutsrunda. Ren logik: priser skickas in, inget hämtas här.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .portfolio import REASON_STOP_LOSS, REASON_TAKE_PROFIT, Portfolio

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LevelHit:
    ticker: str
    reason: str          # REASON_STOP_LOSS eller REASON_TAKE_PROFIT
    price: float         # priset som utlöste
    level: float         # nivån som bröts

    def describe(self) -> str:
        ord_ = "stopp" if self.reason == REASON_STOP_LOSS else "mal"
        return "{}: {} {:.4f} brutet av pris {:.4f}".format(
            self.ticker, ord_, self.level, self.price
        )


def check_levels(portfolio: Portfolio, prices: dict) -> list:
    """Vilka positioner har brutit sitt stopp eller nått sitt mål?

    Saknas pris för ett innehav kan nivån inte kontrolleras. Då loggas det och
    positionen lämnas i fred — att gissa ett pris vore värre än att vänta en
    runda. Bryts stopp och mål samtidigt vinner stoppet: den försiktiga tolkningen.
    """
    hits = []
    for ticker in sorted(portfolio.positions):
        pos = portfolio.positions[ticker]
        price = prices.get(ticker)
        if price is None:
            log.warning(
                "kan inte kontrollera stopp/mal for %s: pris saknas", ticker
            )
            continue

        if pos.stop_loss > 0 and price <= pos.stop_loss:
            hits.append(LevelHit(ticker, REASON_STOP_LOSS, price, pos.stop_loss))
        elif pos.take_profit > 0 and price >= pos.take_profit:
            hits.append(LevelHit(ticker, REASON_TAKE_PROFIT, price, pos.take_profit))

    return hits


def apply_hits(
    portfolio: Portfolio,
    hits: list,
    prices: dict,
    fees,
    timestamp: str,
) -> list:
    """Stäng varje träffad position och returnera affärerna."""
    trades = []
    for hit in hits:
        trade = portfolio.close(
            hit.ticker,
            prices[hit.ticker],
            fees,
            timestamp,
            reason=hit.reason,
        )
        log.info("stangde %s: %s", hit.ticker, hit.describe())
        trades.append(trade)
    return trades


def levels_are_sane(
    price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
) -> bool:
    """Ligger stopp och mål på rätt sida om priset för ett köp?"""
    if stop_loss is None or take_profit is None:
        return False
    if stop_loss <= 0 or take_profit <= 0:
        return False
    return stop_loss < price < take_profit
