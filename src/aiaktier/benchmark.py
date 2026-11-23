"""OMXS30 omräknat till startkapitalet, och dagliga värden.

Indexkurvan normaliseras mot experimentets startdag:

    index_värde(t) = startkapital × (OMX_stängning(t) / OMX_stängning(start))

Startkursen fryses i meta.json vid första körningen. Ändras den i efterhand går
jämförelsen inte att lita på, så koden skriver aldrig om den.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .fees import round_money, round_price


class BenchmarkError(Exception):
    pass


def index_value(start_index: float, current_index: float, start_capital: float) -> float:
    """Vad startkapitalet hade varit värt om det följt index."""
    if start_index <= 0:
        raise BenchmarkError("startkurs for index maste vara positiv")
    if current_index <= 0:
        raise BenchmarkError("aktuell indexkurs maste vara positiv")
    return round_money(start_capital * (current_index / start_index))


def pct_change(current: float, reference: float) -> Optional[float]:
    """Procentuell förändring, eller None när referensen saknas."""
    if reference is None or reference == 0:
        return None
    return round((current / reference - 1.0) * 100.0, 2)


@dataclass(frozen=True)
class DailyValue:
    day: str
    portfolio_value: float
    cash: float
    market_value: float
    index_level: float
    index_value: float
    slot: str
    official_close: bool = False
    holdings: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "date": self.day,
            "portfolio_value": round_money(self.portfolio_value),
            "cash": round_money(self.cash),
            "market_value": round_money(self.market_value),
            "index_level": round_price(self.index_level),
            "index_value": round_money(self.index_value),
            "slot": self.slot,
            "official_close": self.official_close,
            # Innehaven sparas för att dagen ska gå att räkna om mot officiella
            # stängningskurser nästa morgon.
            "holdings": dict(self.holdings or {}),
        }


def upsert_day(entries: list, entry: DailyValue) -> list:
    """Lägg in eller ersätt dagens värde och håll listan sorterad på datum.

    En dag som redan är markerad som officiell stängning skrivs inte över av en
    senare intradagsmätning — annars hade gårdagens korrekta siffra kunnat
    ersättas av en sämre.
    """
    out = [dict(e) for e in entries]
    new = entry.to_dict()

    for index, existing in enumerate(out):
        if existing.get("date") == new["date"]:
            if existing.get("official_close") and not new["official_close"]:
                return out
            out[index] = new
            break
    else:
        out.append(new)

    out.sort(key=lambda e: e["date"])
    return out


def backfill_official_close(
    entries: list,
    day: str,
    stock_closes: dict,
    index_close: float,
    start_capital: float,
    index_start_level: float,
) -> list:
    """Räkna om en dag mot officiella stängningskurser.

    17:00-sloten ligger före börsens stängning 17:30, så dagens sista mätning är
    inte dagens stängningskurs. Nästa morgon finns de riktiga siffrorna, och då
    rättas gårdagen i efterhand — med de innehav som faktiskt fanns den dagen,
    inte dagens.

    Saknas stängningskurs för något av dagens innehav lämnas dagen orörd. Att
    räkna om halva portföljen vore sämre än att låta intradagsvärdet stå kvar.
    """
    out = [dict(e) for e in entries]

    for index, existing in enumerate(out):
        if existing.get("date") != day:
            continue
        if existing.get("official_close"):
            return out                      # redan rättad

        holdings = existing.get("holdings") or {}
        market_value = 0.0
        for ticker, shares in holdings.items():
            close = stock_closes.get(ticker)
            if close is None or close <= 0:
                return out                  # ofullständigt underlag
            market_value += shares * close

        cash = float(existing.get("cash", 0.0))
        out[index] = DailyValue(
            day=day,
            portfolio_value=cash + market_value,
            cash=cash,
            market_value=market_value,
            index_level=index_close,
            index_value=index_value(index_start_level, index_close, start_capital),
            slot="backfill",
            official_close=True,
            holdings=holdings,
        ).to_dict()
        return out

    return out


def latest(entries: list) -> Optional[dict]:
    return entries[-1] if entries else None


def previous(entries: list) -> Optional[dict]:
    return entries[-2] if len(entries) >= 2 else None


def performance(entries: list, start_capital: float) -> dict:
    """Sammanfattning: resultat sedan start och för idag, portfölj mot index."""
    now = latest(entries)
    if now is None:
        return {
            "portfolio_value": round_money(start_capital),
            "index_value": round_money(start_capital),
            "total_return_pct": 0.0,
            "index_return_pct": 0.0,
            "excess_pct": 0.0,
            "today_pct": None,
            "index_today_pct": None,
            "today_excess_pct": None,
        }

    before = previous(entries)
    total = pct_change(now["portfolio_value"], start_capital) or 0.0
    index_total = pct_change(now["index_value"], start_capital) or 0.0

    today = index_today = today_excess = None
    if before is not None:
        today = pct_change(now["portfolio_value"], before["portfolio_value"])
        index_today = pct_change(now["index_value"], before["index_value"])
        if today is not None and index_today is not None:
            today_excess = round(today - index_today, 2)

    return {
        "portfolio_value": round_money(now["portfolio_value"]),
        "index_value": round_money(now["index_value"]),
        "total_return_pct": total,
        "index_return_pct": index_total,
        "excess_pct": round(total - index_total, 2),
        "today_pct": today,
        "index_today_pct": index_today,
        "today_excess_pct": today_excess,
    }
