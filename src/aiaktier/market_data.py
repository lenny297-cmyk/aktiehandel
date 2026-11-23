"""Marknadsdata via yfinance.

Allt som kan gå fel med datakällan går fel här, tydligt och högt. Modulen
gissar aldrig ett pris: saknas data kastas DataUnavailable och rundan hoppas
över längre upp. Ingen lookahead — rader efter körningens tidpunkt kastas bort.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)


class DataUnavailable(Exception):
    """Datakällan gick inte att lita på. Rundan ska hoppas över."""


@dataclass(frozen=True)
class Bar:
    day: date
    close: float

    def to_dict(self) -> dict:
        return {"date": self.day.isoformat(), "close": round(self.close, 4)}


@dataclass(frozen=True)
class MarketSnapshot:
    """Priser och historik fram till och med körningens tidpunkt."""

    as_of: datetime
    prices: dict                      # ticker -> senaste tillgängliga pris
    history: dict                     # ticker -> list[Bar], äldst först
    benchmark_ticker: str
    benchmark_price: float
    benchmark_history: Sequence = field(default_factory=tuple)
    missing: Sequence = field(default_factory=tuple)

    def last_bar_date(self, ticker: str) -> Optional[date]:
        bars = self.history.get(ticker)
        return bars[-1].day if bars else None

    def previous_close(self, ticker: str) -> Optional[float]:
        """Stängningen före den senaste baren, för dagsförändring."""
        bars = self.history.get(ticker, [])
        return bars[-2].close if len(bars) >= 2 else None

    def benchmark_previous_close(self) -> Optional[float]:
        bars = self.benchmark_history
        return bars[-2].close if len(bars) >= 2 else None

    def available(self, tickers: Iterable) -> list:
        return [t for t in tickers if t in self.prices]


def _close_series(frame, symbol: str, multi: bool):
    """Plocka ut stängningskurser för en ticker ur yfinance-svaret."""
    import pandas as pd

    try:
        if multi:
            if symbol not in frame.columns.get_level_values(0):
                return None
            series = frame[symbol]["Close"]
        else:
            series = frame["Close"]
    except (KeyError, IndexError):
        return None

    if series is None or not isinstance(series, pd.Series):
        return None
    return series.dropna()


def _to_bars(series, cutoff: date) -> list:
    """Gör om en pandas-serie till barer, utan rader efter körningens datum."""
    bars = []
    for index, value in series.items():
        day = index.date() if hasattr(index, "date") else index
        if day > cutoff:
            continue          # ingen lookahead
        close = float(value)
        if close <= 0:
            continue
        bars.append(Bar(day, close))
    return bars


def fetch_snapshot(
    tickers: Sequence,
    benchmark: str,
    *,
    now: datetime,
    history_days: int = 60,
    min_coverage: float = 0.5,
    downloader=None,
) -> MarketSnapshot:
    """Hämta priser och historik.

    Kastar DataUnavailable om index saknas eller om för få aktier fick data.
    `downloader` finns för att kunna köra tester utan nätverk.
    """
    symbols = list(dict.fromkeys(list(tickers) + [benchmark]))
    # Kalenderdagar måste täcka önskat antal handelsdagar med marginal.
    period_days = max(history_days * 2 + 15, 40)

    if downloader is None:
        downloader = _yfinance_download

    try:
        frame = downloader(symbols, period_days)
    except DataUnavailable:
        raise
    except Exception as exc:  # nätverksfel, API-ändringar, allt annat
        raise DataUnavailable(
            "kunde inte hamta kursdata: {}: {}".format(type(exc).__name__, exc)
        ) from exc

    if frame is None or len(frame) == 0:
        raise DataUnavailable("kursdata kom tillbaka tom")

    multi = hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1
    cutoff = now.date()

    prices = {}
    history = {}
    missing = []

    for symbol in symbols:
        series = _close_series(frame, symbol, multi)
        if series is None or series.empty:
            missing.append(symbol)
            continue
        bars = _to_bars(series, cutoff)
        if not bars:
            missing.append(symbol)
            continue
        history[symbol] = bars[-history_days:] if history_days > 0 else bars
        prices[symbol] = bars[-1].close

    if benchmark not in prices:
        raise DataUnavailable(
            "jamforelseindex {} saknar kursdata".format(benchmark)
        )

    stock_symbols = [s for s in symbols if s != benchmark]
    covered = sum(1 for s in stock_symbols if s in prices)
    if stock_symbols and covered < len(stock_symbols) * min_coverage:
        raise DataUnavailable(
            "bara {} av {} aktier fick kursdata".format(covered, len(stock_symbols))
        )

    if missing:
        log.warning("saknar kursdata for: %s", ", ".join(sorted(missing)))

    benchmark_history = history.pop(benchmark)
    benchmark_price = prices.pop(benchmark)

    return MarketSnapshot(
        as_of=now,
        prices=prices,
        history=history,
        benchmark_ticker=benchmark,
        benchmark_price=benchmark_price,
        benchmark_history=tuple(benchmark_history),
        missing=tuple(sorted(missing)),
    )


def _yfinance_download(symbols: Sequence, period_days: int):
    import yfinance as yf

    return yf.download(
        list(symbols),
        period="{}d".format(period_days),
        interval="1d",
        auto_adjust=False,
        actions=False,
        progress=False,
        group_by="ticker",
        threads=True,
        # Tvinga samma kolumnform oavsett antal tickers, så att uppackningen
        # nedan inte beror på hur många symboler som råkar efterfrågas.
        multi_level_index=True,
    )


def check_staleness(
    snapshot: MarketSnapshot,
    calendar,
    max_staleness_days: int,
) -> list:
    """Vilka tickers har för gammal senaste kurs?

    Mäts i handelsdagar, så en helg eller helgdag räknas inte som gammal data.
    """
    stale = []
    reference = calendar.localize(snapshot.as_of).date()
    for ticker in sorted(snapshot.prices):
        last = snapshot.last_bar_date(ticker)
        if last is None:
            stale.append(ticker)
            continue
        if calendar.trading_days_between(last, reference) > max_staleness_days:
            stale.append(ticker)
    return stale
