"""Bokföringen.

Ren logik: inga nätverksanrop, ingen klocka, inga filskrivningar. Tidsstämplar
och priser skickas in utifrån. Det är den här modulen som enhetstestas hårdast,
eftersom allt annat vilar på att den stämmer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

from .fees import FeeModel, round_money, round_price


class BookkeepingError(Exception):
    """Basfel för allt som gör en affär omöjlig att bokföra."""


class InsufficientCash(BookkeepingError):
    pass


class NoSuchPosition(BookkeepingError):
    pass


class InsufficientShares(BookkeepingError):
    pass


# Orsaker till att en position öppnas eller stängs.
REASON_DECISION = "decision"
REASON_STOP_LOSS = "stop_loss"
REASON_TAKE_PROFIT = "take_profit"


@dataclass(frozen=True)
class Position:
    ticker: str
    shares: int
    cost_basis: float      # totalt investerat, courtage inräknat
    stop_loss: float
    take_profit: float
    opened_at: str
    rationale: str
    decision_id: str

    @property
    def avg_price(self) -> float:
        """Genomsnittligt anskaffningspris per aktie, courtage inräknat."""
        if self.shares == 0:
            return 0.0
        return round_price(self.cost_basis / self.shares)

    def market_value(self, price: float) -> float:
        return round_money(self.shares * price)

    def unrealized_pnl(self, price: float) -> float:
        return round_money(self.market_value(price) - self.cost_basis)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "shares": self.shares,
            "cost_basis": self.cost_basis,
            "avg_price": self.avg_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "opened_at": self.opened_at,
            "rationale": self.rationale,
            "decision_id": self.decision_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            ticker=d["ticker"],
            shares=int(d["shares"]),
            cost_basis=float(d["cost_basis"]),
            stop_loss=float(d["stop_loss"]),
            take_profit=float(d["take_profit"]),
            opened_at=d.get("opened_at", ""),
            rationale=d.get("rationale", ""),
            decision_id=d.get("decision_id", ""),
        )


@dataclass(frozen=True)
class Trade:
    trade_id: str
    timestamp: str
    action: str            # "buy" eller "sell"
    ticker: str
    shares: int
    price: float           # faktiskt fyllningspris, spread inräknad
    mid_price: float       # marknadspris före spread
    gross: float
    commission: float
    cash_delta: float      # negativ vid köp, positiv vid sälj
    reason: str
    rationale: str
    decision_id: str
    realized_pnl: Optional[float] = None

    def to_dict(self) -> dict:
        d = {
            "trade_id": self.trade_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "ticker": self.ticker,
            "shares": self.shares,
            "price": self.price,
            "mid_price": self.mid_price,
            "gross": self.gross,
            "commission": self.commission,
            "cash_delta": self.cash_delta,
            "reason": self.reason,
            "rationale": self.rationale,
            "decision_id": self.decision_id,
        }
        if self.realized_pnl is not None:
            d["realized_pnl"] = self.realized_pnl
        return d


@dataclass
class Portfolio:
    """Kassa plus öppna positioner. Muterar sig själv, men rör inget utanför."""

    cash: float
    positions: dict = field(default_factory=dict)
    realized_pnl: float = 0.0
    trade_seq: int = 0

    # ---------- värdering ----------

    def market_value(self, prices: dict) -> float:
        """Värdet av innehaven.

        Saknas pris för ett innehav kastas KeyError. Hellre ett tydligt fel än
        en portfölj som tyst värderas till hälften.
        """
        total = 0.0
        for ticker, pos in self.positions.items():
            total += pos.shares * prices[ticker]
        return round_money(total)

    def total_value(self, prices: dict) -> float:
        return round_money(self.cash + self.market_value(prices))

    def position_weight(self, ticker: str, prices: dict) -> float:
        """Positionens andel av totalt portföljvärde, 0.0-1.0."""
        total = self.total_value(prices)
        if total <= 0:
            return 0.0
        pos = self.positions.get(ticker)
        if pos is None:
            return 0.0
        return pos.market_value(prices[ticker]) / total

    # ---------- affärer ----------

    def _next_trade_id(self, timestamp: str) -> str:
        self.trade_seq += 1
        return "{}-{:04d}".format(timestamp[:10], self.trade_seq)

    def buy(
        self,
        ticker: str,
        amount_sek: float,
        mid_price: float,
        stop_loss: float,
        take_profit: float,
        fees: FeeModel,
        timestamp: str,
        rationale: str = "",
        decision_id: str = "",
    ) -> Trade:
        """Köp för högst `amount_sek`, begränsat av kassan. Endast hela aktier."""
        if amount_sek <= 0:
            raise BookkeepingError("belopp maste vara positivt")

        budget = min(round_money(amount_sek), self.cash)
        shares = fees.max_shares_for_budget(budget, mid_price)
        if shares <= 0:
            raise InsufficientCash(
                "otillracklig kassa for {}: budget {:.2f} kr racker inte till "
                "en aktie a {:.4f} kr".format(
                    ticker, budget, fees.buy_fill_price(mid_price)
                )
            )

        fill = fees.buy_fill_price(mid_price)
        gross = round_money(shares * fill)
        commission = fees.commission(gross)
        total = round_money(gross + commission)

        if total > self.cash + 1e-9:
            raise InsufficientCash(
                "otillracklig kassa for {}: {:.2f} kr kravs, {:.2f} kr finns".format(
                    ticker, total, self.cash
                )
            )

        self.cash = round_money(self.cash - total)

        existing = self.positions.get(ticker)
        if existing is None:
            self.positions[ticker] = Position(
                ticker=ticker,
                shares=shares,
                cost_basis=total,
                stop_loss=round_price(stop_loss),
                take_profit=round_price(take_profit),
                opened_at=timestamp,
                rationale=rationale,
                decision_id=decision_id,
            )
        else:
            # Påfyllning: anskaffningsvärdet slås ihop och nivåerna skrivs om
            # till det senaste beslutets, eftersom det är de som gäller nu.
            self.positions[ticker] = replace(
                existing,
                shares=existing.shares + shares,
                cost_basis=round_money(existing.cost_basis + total),
                stop_loss=round_price(stop_loss),
                take_profit=round_price(take_profit),
                rationale=rationale or existing.rationale,
                decision_id=decision_id or existing.decision_id,
            )

        return Trade(
            trade_id=self._next_trade_id(timestamp),
            timestamp=timestamp,
            action="buy",
            ticker=ticker,
            shares=shares,
            price=fill,
            mid_price=round_price(mid_price),
            gross=gross,
            commission=commission,
            cash_delta=round_money(-total),
            reason=REASON_DECISION,
            rationale=rationale,
            decision_id=decision_id,
        )

    def sell(
        self,
        ticker: str,
        shares: int,
        mid_price: float,
        fees: FeeModel,
        timestamp: str,
        reason: str = REASON_DECISION,
        rationale: str = "",
        decision_id: str = "",
    ) -> Trade:
        """Sälj ett antal aktier ur ett befintligt innehav."""
        pos = self.positions.get(ticker)
        if pos is None:
            raise NoSuchPosition("inget innehav i {}".format(ticker))
        if shares <= 0:
            raise BookkeepingError("antal maste vara positivt")
        if shares > pos.shares:
            raise InsufficientShares(
                "kan inte salja {} {}, innehavet ar {}".format(
                    shares, ticker, pos.shares
                )
            )

        fill = fees.sell_fill_price(mid_price)
        gross = round_money(shares * fill)
        commission = fees.commission(gross)
        net = round_money(gross - commission)

        # Anskaffningsvärdet fördelas proportionellt över de sålda aktierna.
        cost_portion = round_money(pos.cost_basis * shares / pos.shares)
        realized = round_money(net - cost_portion)

        self.cash = round_money(self.cash + net)
        self.realized_pnl = round_money(self.realized_pnl + realized)

        if shares == pos.shares:
            del self.positions[ticker]
        else:
            self.positions[ticker] = replace(
                pos,
                shares=pos.shares - shares,
                cost_basis=round_money(pos.cost_basis - cost_portion),
            )

        return Trade(
            trade_id=self._next_trade_id(timestamp),
            timestamp=timestamp,
            action="sell",
            ticker=ticker,
            shares=shares,
            price=fill,
            mid_price=round_price(mid_price),
            gross=gross,
            commission=commission,
            cash_delta=net,
            reason=reason,
            rationale=rationale or pos.rationale,
            decision_id=decision_id or pos.decision_id,
            realized_pnl=realized,
        )

    def close(
        self,
        ticker: str,
        mid_price: float,
        fees: FeeModel,
        timestamp: str,
        reason: str = REASON_DECISION,
        rationale: str = "",
        decision_id: str = "",
    ) -> Trade:
        """Stäng hela innehavet i en aktie."""
        pos = self.positions.get(ticker)
        if pos is None:
            raise NoSuchPosition("inget innehav i {}".format(ticker))
        return self.sell(
            ticker,
            pos.shares,
            mid_price,
            fees,
            timestamp,
            reason=reason,
            rationale=rationale,
            decision_id=decision_id,
        )

    # ---------- serialisering ----------

    def sorted_positions(self) -> list:
        return [self.positions[t] for t in sorted(self.positions)]

    def to_dict(self, prices: Optional[dict] = None) -> dict:
        ordered = self.sorted_positions()
        d = {
            "cash": round_money(self.cash),
            "realized_pnl": round_money(self.realized_pnl),
            "trade_seq": self.trade_seq,
            "positions": [p.to_dict() for p in ordered],
        }
        if prices is not None:
            for pos, entry in zip(ordered, d["positions"]):
                price = prices.get(pos.ticker)
                if price is not None:
                    entry["last_price"] = round_price(price)
                    entry["market_value"] = pos.market_value(price)
                    entry["unrealized_pnl"] = pos.unrealized_pnl(price)
            d["market_value"] = self.market_value(prices)
            d["total_value"] = self.total_value(prices)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Portfolio":
        positions = {}
        for raw in d.get("positions", []):
            pos = Position.from_dict(raw)
            positions[pos.ticker] = pos
        return cls(
            cash=float(d["cash"]),
            positions=positions,
            realized_pnl=float(d.get("realized_pnl", 0.0)),
            trade_seq=int(d.get("trade_seq", 0)),
        )

    @classmethod
    def new(cls, start_capital: float) -> "Portfolio":
        return cls(cash=round_money(start_capital))

    def tickers(self) -> Iterable:
        return sorted(self.positions)
