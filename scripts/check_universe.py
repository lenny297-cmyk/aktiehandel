"""Kontrollera att varje ticker i data/universe.json ger kursdata.

OMXS30 revideras halvårsvis i januari och juli. Kör det här efter varje
revidering, och när som helst om en ticker börjar bete sig konstigt.

    python scripts/check_universe.py

Avslutar med kod 1 om någon ticker saknar data, så att det går att köra i CI.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiaktier.config import Config  # noqa: E402
from aiaktier.market_data import DataUnavailable, fetch_snapshot  # noqa: E402


def main() -> int:
    config = Config.load(ROOT)
    raw = json.loads((ROOT / "data" / "universe.json").read_text(encoding="utf-8"))
    tickers = [entry["ticker"] for entry in raw["constituents"]]
    names = {e["ticker"]: e.get("name", e["ticker"]) for e in raw["constituents"]}

    print("Universum: {} ({} tickers, as_of {})".format(
        raw.get("index", "?"), len(tickers), raw.get("as_of", "?")
    ))
    if len(tickers) != 30:
        print("VARNING: OMXS30 ska ha 30 bolag, listan har {}.".format(len(tickers)))

    try:
        snapshot = fetch_snapshot(
            tickers,
            config.benchmark_ticker,
            now=datetime.now(timezone.utc),
            history_days=5,
            min_coverage=0.0,       # rapportera allt i stället för att avbryta
        )
    except DataUnavailable as exc:
        print("Kunde inte hamta kursdata: {}".format(exc))
        return 1

    print("\nIndex {}: {:.2f}".format(config.benchmark_ticker, snapshot.benchmark_price))
    print()

    saknas = []
    for ticker in tickers:
        price = snapshot.prices.get(ticker)
        if price is None:
            saknas.append(ticker)
            print("  SAKNAS  {:<14} {}".format(ticker, names[ticker]))
        else:
            print("  ok      {:<14} {:<22} {:>10.2f}  senast {}".format(
                ticker, names[ticker], price, snapshot.last_bar_date(ticker)
            ))

    if saknas:
        print(
            "\n{} ticker(s) saknar kursdata. Kontrollera stavningen mot Yahoo "
            "Finance och mot Nasdaqs aktuella OMXS30-sammansattning.".format(len(saknas))
        )
        return 1

    print("\nAlla {} tickers gav kursdata.".format(len(tickers)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
