"""Orkestreringen: en körning från start till sparad state.

Ordningen i varje runda:

    1. avgör om körningen hör till en giltig slot
    2. hämta marknadsdata, annars hoppa över rundan
    3. rätta gårdagens värde mot officiella stängningskurser
    4. kontrollera stopp och mål
    5. värdera portföljen
    6. fråga Claude
    7. validera förslagen
    8. bokför de godkända
    9. skriv state och publicera till /docs

Steg 6 och 7 kan falla bort utan att rundan går förlorad: stoppkontrollen och
dagens värde sparas ändå. Däremot avbryts allt om marknadsdatan sviker, för då
finns det ingenting att vara säker på.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import benchmark, report, stops
from .config import Config
from .decide import DecisionContext, DecisionUnavailable, ask, summarize_history
from .market_data import DataUnavailable, check_staleness, fetch_snapshot
from .portfolio import Portfolio
from .state import make_store
from .trading_calendar import already_ran, record_run
from .validator import validate_proposals

log = logging.getLogger("aiaktier")

EXIT_OK = 0
EXIT_ERROR = 1


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    config = Config.load(args.root)
    store = make_store(args.dry_run)
    now = parse_now(args.now)

    if store.dry_run:
        log.info("DRY RUN: ingenting skrivs till disk och ingenting committas")

    calendar = config.calendar
    slot_name = resolve_slot(calendar, now, args)
    if slot_name is None:
        return EXIT_OK

    runs = store.read(config.paths.runs, {}) or {}
    local_day = calendar.localize(now).date()
    if already_ran(runs, local_day, slot_name) and not args.force:
        log.info("sloten %s har redan kort %s, hoppar over", slot_name, local_day)
        return EXIT_OK

    universe = config.universe()
    names = config.universe_names()

    try:
        snapshot = fetch_snapshot(
            universe,
            config.benchmark_ticker,
            now=now,
            history_days=config.history_days,
            attempts=config.download_attempts,
            backoff_seconds=config.download_backoff_seconds,
            backoff_factor=config.download_backoff_factor,
        )
    except DataUnavailable as exc:
        log.error("hoppar over rundan: %s", exc)
        return EXIT_OK

    stale = check_staleness(snapshot, calendar, config.max_staleness_days)
    prices = {t: p for t, p in snapshot.prices.items() if t not in stale}
    if stale:
        log.warning("for gammal kursdata, utesluts: %s", ", ".join(stale))

    portfolio = load_portfolio(store, config)

    missing_held = [t for t in portfolio.positions if t not in prices]
    if missing_held:
        log.error(
            "hoppar over rundan: saknar fardsk kurs for innehav %s. "
            "Portfoljen gar inte att vardera utan att gissa.",
            ", ".join(missing_held),
        )
        return EXIT_OK

    meta = ensure_meta(store, config, snapshot, now)
    daily_values = store.read(config.paths.daily_values, []) or []
    trades = store.read(config.paths.trades, []) or []
    decisions = store.read(config.paths.decisions, []) or []

    timestamp = calendar.localize(now).isoformat(timespec="seconds")

    # ---- 3. rätta gårdagen mot officiella stängningskurser ----
    daily_values = backfill_previous_day(
        daily_values, calendar, snapshot, meta, local_day
    )

    # ---- 4. stopp och mål ----
    hits = stops.check_levels(portfolio, prices)
    new_trades = stops.apply_hits(portfolio, hits, prices, config.fees, timestamp)

    # ---- 5-7. beslut och validering ----
    decision_record = None
    try:
        decision = ask(
            build_context(
                config, snapshot, portfolio, prices, names, meta,
                daily_values, trades, decisions, slot_name, timestamp,
            ),
            config.model,
        )
    except DecisionUnavailable as exc:
        log.error("inget beslut den har rundan: %s", exc)
        decision = None
    except Exception as exc:  # oväntat, men ska inte ta ner hela körningen
        log.exception("ovantat fel i beslutslagret: %s", exc)
        decision = None

    if decision is not None:
        result = validate_proposals(
            decision.proposals,
            portfolio=portfolio,
            prices=prices,
            universe=universe,
            fees=config.fees,
            max_position_pct=config.max_position_pct,
            min_trade_sek=config.min_trade_sek,
            max_proposals=config.max_proposals,
        )
        decision_id = "{}-{}".format(local_day.isoformat(), slot_name)
        new_trades += apply_accepted(
            portfolio, result, prices, config.fees, timestamp, decision_id
        )
        decision_record = {
            "decision_id": decision_id,
            "timestamp": timestamp,
            "slot": slot_name,
            "model": decision.model,
            "market_view": decision.market_view,
            "usage": decision.usage,
            **result.to_dict(),
        }
        decisions = list(decisions) + [decision_record]

    # ---- 8-9. värdera, spara, publicera ----
    trades = list(trades) + [t.to_dict() for t in new_trades]

    index_level = snapshot.benchmark_price
    index_value = benchmark.index_value(
        float(meta["index_start_level"]), index_level, float(meta["start_capital"])
    )
    daily_values = benchmark.upsert_day(
        daily_values,
        benchmark.DailyValue(
            day=local_day.isoformat(),
            portfolio_value=portfolio.total_value(prices),
            cash=portfolio.cash,
            market_value=portfolio.market_value(prices),
            index_level=index_level,
            index_value=index_value,
            slot=slot_name,
            official_close=False,
            holdings={t: p.shares for t, p in portfolio.positions.items()},
        ),
    )
    runs = record_run(runs, local_day, slot_name)

    summary = report.build_summary(
        generated_at=timestamp,
        slot=slot_name,
        meta=meta,
        portfolio=portfolio,
        prices=prices,
        daily_values=daily_values,
        trades=trades,
        decision=decision_record,
        names=names,
        index_level=index_level,
        index_value=index_value,
        skipped=None,
    )

    store.write(config.paths.portfolio, portfolio.to_dict(prices))
    store.write(config.paths.trades, trades)
    store.write(config.paths.daily_values, daily_values)
    store.write(config.paths.decisions, decisions)
    store.write(config.paths.runs, runs)
    store.write(config.paths.summary, summary)
    report.publish(store, config.paths)

    log_result(portfolio, prices, summary, new_trades, store.dry_run)
    return EXIT_OK


# ------------------------------------------------------------ delsteg


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="aiaktier",
        description="Simulerat AI-aktieexperiment. Inga riktiga order laggs.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="kor hela flodet utan att skriva nagot till disk",
    )
    parser.add_argument(
        "--slot", default=None,
        help="kor en namngiven slot oavsett klockslag (morning/midday/close)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="kor aven om sloten redan ar kord i dag",
    )
    parser.add_argument(
        "--now", default=None,
        help="ISO-tidpunkt att kora som, for felsokning",
    )
    parser.add_argument("--root", default=None, help="sokvag till repots rot")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def parse_now(value):
    if value is None:
        return datetime.now(timezone.utc)
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def resolve_slot(calendar, now, args):
    """Vilken slot hör körningen till? None betyder att rundan ska hoppas över."""
    if args.slot:
        valid = [s.name for s in calendar.slots]
        if args.slot not in valid:
            log.error("okand slot %r, valj mellan %s", args.slot, ", ".join(valid))
            return None
        log.info("kor slot %s pa begaran", args.slot)
        return args.slot

    decision = calendar.match_slot(now)
    if not decision.ok:
        log.info(
            "hoppar over: %s (lokal tid %s)",
            decision.reason,
            decision.local_time.isoformat(timespec="minutes"),
        )
        return None

    log.info(
        "slot %s, lokal tid %s",
        decision.slot_name,
        decision.local_time.isoformat(timespec="minutes"),
    )
    return decision.slot_name


def load_portfolio(store, config) -> Portfolio:
    raw = store.read(config.paths.portfolio)
    if raw is None:
        log.info("ingen portfolj pa disk, startar med %s kr", config.start_capital)
        return Portfolio.new(config.start_capital)
    return Portfolio.from_dict(raw)


def ensure_meta(store, config, snapshot, now) -> dict:
    """Startdag, startkapital och indexets startkurs fryses vid första körningen."""
    meta = store.read(config.paths.meta)
    if meta:
        return meta

    local_day = config.calendar.localize(now).date()
    meta = {
        "start_date": local_day.isoformat(),
        "start_capital": config.start_capital,
        "benchmark_ticker": config.benchmark_ticker,
        "index_start_level": snapshot.benchmark_price,
        "created_at": config.calendar.localize(now).isoformat(timespec="seconds"),
        "note": (
            "Fryst vid forsta korningen. Andras aldrig av koden; en andring har "
            "gor jamforelsen mot index omojlig att lita pa."
        ),
    }
    store.write(config.paths.meta, meta)
    log.info(
        "experimentet startar %s med %s kr, index %s pa %.2f",
        meta["start_date"], meta["start_capital"],
        meta["benchmark_ticker"], meta["index_start_level"],
    )
    return meta


def backfill_previous_day(daily_values, calendar, snapshot, meta, local_day):
    """Rätta senaste ofullständiga dagen mot officiella stängningskurser."""
    if not daily_values:
        return daily_values

    last = daily_values[-1]
    if last.get("official_close") or last.get("date") == local_day.isoformat():
        return daily_values

    day = last["date"]
    closes = {}
    for ticker, bars in snapshot.history.items():
        for bar in reversed(bars):
            if bar.day.isoformat() == day:
                closes[ticker] = bar.close
                break

    index_close = None
    for bar in reversed(snapshot.benchmark_history):
        if bar.day.isoformat() == day:
            index_close = bar.close
            break
    if index_close is None:
        log.info("ingen officiell indexstangning for %s an, lamnar dagen", day)
        return daily_values

    updated = benchmark.backfill_official_close(
        daily_values,
        day,
        closes,
        index_close,
        float(meta["start_capital"]),
        float(meta["index_start_level"]),
    )
    if updated != daily_values:
        log.info("rattade %s mot officiella stangningskurser", day)
    return updated


def build_context(
    config, snapshot, portfolio, prices, names, meta,
    daily_values, trades, decisions, slot_name, timestamp,
) -> DecisionContext:
    total_value = portfolio.total_value(prices)
    index_level = snapshot.benchmark_price
    index_value = benchmark.index_value(
        float(meta["index_start_level"]), index_level, float(meta["start_capital"])
    )

    positions = []
    for pos in portfolio.sorted_positions():
        price = prices[pos.ticker]
        positions.append({
            "ticker": pos.ticker,
            "name": names.get(pos.ticker, pos.ticker),
            "shares": pos.shares,
            "avg": pos.avg_price,
            "price": price,
            "value": pos.market_value(price),
            "weight": (pos.market_value(price) / total_value * 100.0) if total_value else 0.0,
            "pnl": pos.unrealized_pnl(price),
            "stop": pos.stop_loss,
            "target": pos.take_profit,
            "why": pos.rationale or "(ingen sparad)",
        })

    market = {
        ticker: summarize_history(bars)
        for ticker, bars in snapshot.history.items()
        if ticker in prices
    }

    recent = []
    for trade in list(trades)[-6:]:
        if trade.get("action") != "sell":
            continue
        pnl = trade.get("realized_pnl")
        recent.append({
            "date": str(trade.get("timestamp", ""))[:10],
            "action": trade.get("action"),
            "ticker": trade.get("ticker"),
            "result": "{:+.0f} kr".format(pnl) if pnl is not None else "-",
            "reason": trade.get("reason", "-"),
            "why": trade.get("rationale", ""),
        })

    rejections = []
    if decisions:
        for rejection in decisions[-1].get("rejected", []):
            proposal = rejection.get("proposal", {})
            rejections.append({
                "action": proposal.get("action", "?"),
                "ticker": proposal.get("ticker", "?"),
                "reason": rejection.get("reason", "?"),
                "detail": rejection.get("detail", ""),
            })

    fees = config.fees
    return DecisionContext(
        slot=slot_name,
        local_time=timestamp,
        cash=portfolio.cash,
        total_value=total_value,
        start_capital=float(meta["start_capital"]),
        index_value=index_value,
        index_level=index_level,
        performance=benchmark.performance(list(daily_values), float(meta["start_capital"])),
        positions=positions,
        market=market,
        names=names,
        recent_trades=recent,
        previous_rejections=rejections,
        max_position_pct=config.max_position_pct,
        min_trade_sek=config.min_trade_sek,
        max_proposals=config.max_proposals,
        commission_pct=fees.commission_pct,
        commission_min_sek=fees.commission_min_sek,
        spread_pct=fees.spread_pct,
    )


def apply_accepted(portfolio, result, prices, fees, timestamp, decision_id) -> list:
    """Bokför de godkända förslagen i samma ordning som de validerades."""
    from .validator import BUY, shares_to_sell

    booked = []
    for proposal in result.accepted:
        price = prices[proposal.ticker]
        if proposal.action == BUY:
            trade = portfolio.buy(
                proposal.ticker,
                proposal.amount_sek,
                price,
                proposal.stop_loss,
                proposal.take_profit,
                fees,
                timestamp,
                rationale=proposal.rationale,
                decision_id=decision_id,
            )
        else:
            held = portfolio.positions[proposal.ticker].shares
            trade = portfolio.sell(
                proposal.ticker,
                shares_to_sell(proposal.amount_sek, price, held),
                price,
                fees,
                timestamp,
                rationale=proposal.rationale,
                decision_id=decision_id,
            )
        log.info(
            "bokforde %s %s st %s a %.2f kr",
            trade.action, trade.shares, trade.ticker, trade.price,
        )
        booked.append(trade)
    return booked


def log_result(portfolio, prices, summary, new_trades, dry_run) -> None:
    perf = summary["performance"]
    prefix = "[dry run] " if dry_run else ""
    log.info(
        "%sportfolj %.2f kr mot index %.2f kr (%+.2f %% mot %+.2f %%), "
        "kassa %.2f kr, %d positioner, %d affarer denna runda",
        prefix,
        summary["total_value"],
        summary["benchmark"]["value_sek"] or 0.0,
        perf.get("total_return_pct") or 0.0,
        perf.get("index_return_pct") or 0.0,
        portfolio.cash,
        len(portfolio.positions),
        len(new_trades),
    )


if __name__ == "__main__":
    sys.exit(main())
