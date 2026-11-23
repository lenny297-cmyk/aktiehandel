"""Valideraren.

Det är här handelsreglerna upprätthålls. Modellens svar är förslag; ingenting
blir en affär förrän det har passerat varenda kontroll nedan. Ogiltiga förslag
loggas med skäl och ignoreras.

Förslagen prövas i ordning mot en kopia av portföljen, så att två köp i samma
runda inte båda kan göra anspråk på samma kassa eller spräcka 20 %-taket
tillsammans.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from .fees import FeeModel, round_money, round_price
from .portfolio import BookkeepingError, InsufficientCash, Portfolio
from .stops import levels_are_sane

log = logging.getLogger(__name__)

BUY = "buy"
SELL = "sell"

# Avslagsskäl. Konstanter, så att både loggen och testerna pekar på samma sak.
REJ_MALFORMED = "felaktigt_format"
REJ_UNKNOWN_ACTION = "okand_atgard"
REJ_NOT_IN_UNIVERSE = "utanfor_universum"
REJ_NO_PRICE = "pris_saknas"
REJ_BAD_AMOUNT = "ogiltigt_belopp"
REJ_BELOW_MIN_TRADE = "under_minsta_affar"
REJ_MISSING_LEVELS = "stopp_eller_mal_saknas"
REJ_ILLOGICAL_LEVELS = "orimliga_nivaer"
REJ_INSUFFICIENT_CASH = "otillracklig_kassa"
REJ_POSITION_CAP = "over_positionstak"
REJ_NO_POSITION = "inget_innehav"
REJ_TOO_MANY = "over_tak_for_antal_forslag"


class ProposalError(ValueError):
    """Förslaget gick inte ens att tolka."""


@dataclass(frozen=True)
class Proposal:
    action: str
    ticker: str
    amount_sek: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    rationale: str

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "ticker": self.ticker,
            "amount_sek": self.amount_sek,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class Rejection:
    proposal: dict
    reason: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"proposal": self.proposal, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class ValidationResult:
    accepted: Sequence = ()
    rejected: Sequence = ()

    def to_dict(self) -> dict:
        return {
            "accepted": [p.to_dict() for p in self.accepted],
            "rejected": [r.to_dict() for r in self.rejected],
        }


def coerce_proposal(raw) -> Proposal:
    """Gör om ett rått förslag till ett Proposal, eller kasta ProposalError."""
    if not isinstance(raw, dict):
        raise ProposalError("forslaget ar inte ett objekt")

    action = str(raw.get("action", "")).strip().lower()
    ticker = str(raw.get("ticker", "")).strip().upper()
    if not ticker:
        raise ProposalError("ticker saknas")

    def number(key):
        value = raw.get(key)
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ProposalError("{} ar inte ett tal: {!r}".format(key, value))

    amount = number("amount_sek")
    if amount is None:
        raise ProposalError("amount_sek saknas")

    return Proposal(
        action=action,
        ticker=ticker,
        amount_sek=amount,
        stop_loss=number("stop_loss"),
        take_profit=number("take_profit"),
        rationale=str(raw.get("rationale", "")).strip(),
    )


def validate_proposals(
    raw_proposals: Sequence,
    *,
    portfolio: Portfolio,
    prices: dict,
    universe: Sequence,
    fees: FeeModel,
    max_position_pct: float = 0.20,
    min_trade_sek: float = 1000.0,
    max_proposals: int = 3,
) -> ValidationResult:
    """Pröva varje förslag mot kassa, positionstak, universum och nivåer."""
    universe_set = set(universe)
    shadow = Portfolio.from_dict(portfolio.to_dict())

    accepted = []
    rejected = []

    for raw in raw_proposals:
        if len(accepted) >= max_proposals:
            rejected.append(
                Rejection(
                    _as_dict(raw),
                    REJ_TOO_MANY,
                    "taket ar {} forslag per korning".format(max_proposals),
                )
            )
            continue

        try:
            proposal = coerce_proposal(raw)
        except ProposalError as exc:
            rejected.append(Rejection(_as_dict(raw), REJ_MALFORMED, str(exc)))
            continue

        problem = _check(
            proposal,
            shadow=shadow,
            prices=prices,
            universe_set=universe_set,
            fees=fees,
            max_position_pct=max_position_pct,
            min_trade_sek=min_trade_sek,
        )
        if problem is not None:
            reason, detail = problem
            rejected.append(Rejection(proposal.to_dict(), reason, detail))
            continue

        # Bokför i skuggkopian så att nästa förslag prövas mot det som redan
        # är godkänt i den här rundan.
        _apply_to_shadow(proposal, shadow, prices, fees)
        accepted.append(proposal)

    for rejection in rejected:
        log.warning(
            "forslag avvisat (%s): %s %s",
            rejection.reason,
            rejection.proposal.get("action"),
            rejection.proposal.get("ticker"),
        )

    return ValidationResult(tuple(accepted), tuple(rejected))


def _as_dict(raw) -> dict:
    return raw if isinstance(raw, dict) else {"raw": repr(raw)}


def _check(
    proposal: Proposal,
    *,
    shadow: Portfolio,
    prices: dict,
    universe_set: set,
    fees: FeeModel,
    max_position_pct: float,
    min_trade_sek: float,
):
    """Returnerar (skäl, detalj) om förslaget ska avvisas, annars None."""
    if proposal.action not in (BUY, SELL):
        return REJ_UNKNOWN_ACTION, proposal.action or "(tom)"

    if proposal.ticker not in universe_set:
        return REJ_NOT_IN_UNIVERSE, proposal.ticker

    price = prices.get(proposal.ticker)
    if price is None or price <= 0:
        return REJ_NO_PRICE, proposal.ticker

    if proposal.amount_sek <= 0:
        return REJ_BAD_AMOUNT, "{}".format(proposal.amount_sek)

    if proposal.action == BUY:
        return _check_buy(
            proposal, shadow, price, fees, max_position_pct, min_trade_sek
        )
    return _check_sell(proposal, shadow, price, min_trade_sek)


def _check_buy(
    proposal: Proposal,
    shadow: Portfolio,
    price: float,
    fees: FeeModel,
    max_position_pct: float,
    min_trade_sek: float,
):
    if proposal.amount_sek < min_trade_sek:
        return REJ_BELOW_MIN_TRADE, "{:.0f} < {:.0f} kr".format(
            proposal.amount_sek, min_trade_sek
        )

    if proposal.stop_loss is None or proposal.take_profit is None:
        return REJ_MISSING_LEVELS, "stop_loss={} take_profit={}".format(
            proposal.stop_loss, proposal.take_profit
        )

    if not levels_are_sane(price, proposal.stop_loss, proposal.take_profit):
        return REJ_ILLOGICAL_LEVELS, (
            "stopp {} och mal {} ar inte pa var sin sida om priset {:.4f}".format(
                proposal.stop_loss, proposal.take_profit, price
            )
        )

    # Kassan måste täcka beloppet inklusive courtage.
    required = round_money(proposal.amount_sek + fees.commission(proposal.amount_sek))
    if required > shadow.cash + 1e-9:
        return REJ_INSUFFICIENT_CASH, "{:.2f} kr kravs, {:.2f} kr finns".format(
            required, shadow.cash
        )

    # Taket mäts på vad som faktiskt skulle bokföras, inte på det begärda
    # beloppet. Därför provbokförs affären i en egen kopia först.
    trial = Portfolio.from_dict(shadow.to_dict())
    try:
        trial.buy(
            proposal.ticker,
            proposal.amount_sek,
            price,
            proposal.stop_loss,
            proposal.take_profit,
            fees,
            "trial",
        )
    except InsufficientCash as exc:
        return REJ_INSUFFICIENT_CASH, str(exc)
    except BookkeepingError as exc:
        return REJ_BAD_AMOUNT, str(exc)

    weight = trial.position_weight(
        proposal.ticker, _shadow_prices(trial, proposal.ticker, price)
    )
    if weight > max_position_pct + 1e-9:
        return REJ_POSITION_CAP, "{:.1f} % > {:.1f} %".format(
            weight * 100, max_position_pct * 100
        )

    return None


def _check_sell(proposal: Proposal, shadow: Portfolio, price: float, min_trade_sek: float):
    pos = shadow.positions.get(proposal.ticker)
    if pos is None or pos.shares <= 0:
        return REJ_NO_POSITION, proposal.ticker

    # Ett sälj får vara mindre än minsta affär om det stänger hela innehavet.
    holding_value = pos.market_value(price)
    if proposal.amount_sek < min_trade_sek and proposal.amount_sek < holding_value:
        return REJ_BELOW_MIN_TRADE, "{:.0f} < {:.0f} kr".format(
            proposal.amount_sek, min_trade_sek
        )

    if shares_to_sell(proposal.amount_sek, price, pos.shares) <= 0:
        return REJ_BAD_AMOUNT, "beloppet motsvarar noll aktier"

    return None


def _shadow_prices(shadow: Portfolio, ticker: str, price: float) -> dict:
    """Priser för skuggportföljens värdering. Saknade priser faller tillbaka
    på anskaffningsvärdet, så att en enstaka lucka inte sänker hela taket."""
    out = {ticker: price}
    for held, pos in shadow.positions.items():
        out.setdefault(held, pos.avg_price)
    return out


def shares_to_sell(amount_sek: float, price: float, held: int) -> int:
    """Hur många aktier ett säljbelopp motsvarar, begränsat av innehavet."""
    if price <= 0 or held <= 0:
        return 0
    shares = int(amount_sek // price)
    # Ett belopp som täcker hela innehavet, eller nästan, stänger positionen.
    if shares >= held or amount_sek >= held * price * 0.995:
        return held
    return max(shares, 0)


def _apply_to_shadow(proposal: Proposal, shadow: Portfolio, prices: dict, fees: FeeModel):
    price = prices[proposal.ticker]
    if proposal.action == BUY:
        shadow.buy(
            proposal.ticker,
            proposal.amount_sek,
            price,
            round_price(proposal.stop_loss),
            round_price(proposal.take_profit),
            fees,
            "shadow",
        )
    else:
        held = shadow.positions[proposal.ticker].shares
        shadow.sell(
            proposal.ticker,
            shares_to_sell(proposal.amount_sek, price, held),
            price,
            fees,
            "shadow",
        )
