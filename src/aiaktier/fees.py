"""Courtage och spread.

Ren räknelogik: inga nätverksanrop, ingen klocka, inga filskrivningar.
"""

from __future__ import annotations

from dataclasses import dataclass

# Priser avrundas till fyra decimaler, pengar till två (öre).
PRICE_DP = 4
MONEY_DP = 2


def round_price(value: float) -> float:
    return round(float(value), PRICE_DP)


def round_money(value: float) -> float:
    return round(float(value), MONEY_DP)


@dataclass(frozen=True)
class FeeModel:
    """Courtagemodell med procentsats, minimiavgift och spread.

    Spread appliceras symmetriskt kring mid: köp fylls över mid, sälj under.
    """

    commission_pct: float = 0.0025
    commission_min_sek: float = 1.0
    spread_pct: float = 0.0010

    def __post_init__(self) -> None:
        for name in ("commission_pct", "commission_min_sek", "spread_pct"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} får inte vara negativ")

    @classmethod
    def from_config(cls, cfg: dict) -> "FeeModel":
        return cls(
            commission_pct=float(cfg.get("commission_pct", 0.0025)),
            commission_min_sek=float(cfg.get("commission_min_sek", 1.0)),
            spread_pct=float(cfg.get("spread_pct", 0.0010)),
        )

    def buy_fill_price(self, mid: float) -> float:
        """Pris ett köp fylls till, dvs mid plus halva spreaden."""
        if mid <= 0:
            raise ValueError("mid måste vara positivt")
        return round_price(mid * (1.0 + self.spread_pct / 2.0))

    def sell_fill_price(self, mid: float) -> float:
        """Pris ett sälj fylls till, dvs mid minus halva spreaden."""
        if mid <= 0:
            raise ValueError("mid måste vara positivt")
        return round_price(mid * (1.0 - self.spread_pct / 2.0))

    def commission(self, gross: float) -> float:
        """Courtage på ett affärsbelopp. Minimiavgiften gäller alltid."""
        if gross <= 0:
            return 0.0
        return round_money(max(self.commission_min_sek, gross * self.commission_pct))

    def buy_cost(self, shares: int, mid: float) -> float:
        """Total utbetalning för ett köp: belopp plus courtage."""
        gross = round_money(shares * self.buy_fill_price(mid))
        return round_money(gross + self.commission(gross))

    def max_shares_for_budget(self, budget: float, mid: float) -> int:
        """Största antal hela aktier vars totalkostnad ryms i budgeten."""
        fill = self.buy_fill_price(mid)
        if budget <= 0 or fill <= 0:
            return 0
        shares = int(budget // fill)
        while shares > 0 and self.buy_cost(shares, mid) > budget + 1e-9:
            shares -= 1
        return shares
