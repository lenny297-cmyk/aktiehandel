"""Hela flödet från slot till sparad state, utan nät och utan API-nyckel."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiaktier import run as run_module
from aiaktier.decide import Decision, DecisionUnavailable
from aiaktier.market_data import Bar, DataUnavailable, MarketSnapshot
from aiaktier.state import read_json

ROOT = Path(__file__).resolve().parents[1]

ERIC = "ERIC-B.ST"
VOLV = "VOLV-B.ST"
BENCH = "^OMX"

# Måndag 23 november 2026, 08:15 UTC = 09:15 svensk tid: morgonsloten.
MORGON = "2026-11-23T08:15:00+00:00"
MIDDAG = "2026-11-23T11:30:00+00:00"


@pytest.fixture
def repo(tmp_path) -> Path:
    """Ett minimalt repo med egen konfiguration och eget universum."""
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "universe.json").write_text(
        json.dumps({
            "index": "OMXS30",
            "as_of": "2026-01-01",
            "constituents": [
                {"ticker": ERIC, "name": "Ericsson B"},
                {"ticker": VOLV, "name": "Volvo B"},
            ],
        }),
        encoding="utf-8",
    )
    return tmp_path


def bars(end: date, closes: list) -> list:
    days = []
    cursor = end
    while len(days) < len(closes):
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    return [Bar(day, close) for day, close in zip(days, closes)]


def snapshot(now_iso=MORGON, eric=100.0, volv=250.0, index=2_500.0):
    now = datetime.fromisoformat(now_iso)
    end = now.date()
    return MarketSnapshot(
        as_of=now,
        prices={ERIC: eric, VOLV: volv},
        history={
            ERIC: bars(end, [95.0, 97.0, 99.0, eric]),
            VOLV: bars(end, [240.0, 245.0, 248.0, volv]),
        },
        benchmark_ticker=BENCH,
        benchmark_price=index,
        benchmark_history=tuple(bars(end, [2_400.0, 2_450.0, 2_480.0, index])),
    )


def fake_decision(proposals, view="lugnt lage"):
    return Decision(
        market_view=view,
        proposals=proposals,
        model="claude-sonnet-5-5",
        raw_text="{}",
        usage={"input_tokens": 10, "output_tokens": 5},
    )


def buy_proposal(ticker=ERIC, amount=15_000, stop=90.0, target=120.0):
    return {
        "action": "buy",
        "ticker": ticker,
        "amount_sek": amount,
        "stop_loss": stop,
        "take_profit": target,
        "rationale": "stigande trend och stark rapport",
    }


@pytest.fixture
def wire(monkeypatch):
    """Koppla in fejkad marknadsdata och ett fejkat modellsvar."""
    def _wire(snap=None, decision=None, data_error=None, decision_error=None):
        def fetch(*args, **kwargs):
            if data_error:
                raise data_error
            return snap if snap is not None else snapshot()

        def ask(*args, **kwargs):
            if decision_error:
                raise decision_error
            return decision if decision is not None else fake_decision([])

        monkeypatch.setattr(run_module, "fetch_snapshot", fetch)
        monkeypatch.setattr(run_module, "ask", ask)
    return _wire


def invoke(repo, *extra, now=MORGON):
    return run_module.main(["--root", str(repo), "--now", now, *extra])


# ------------------------------------------------------- första körningen


def test_forsta_korningen_fryser_start_och_skriver_state(repo, wire):
    wire()
    assert invoke(repo) == 0

    meta = read_json(repo / "data" / "meta.json")
    assert meta["start_date"] == "2026-11-23"
    assert meta["start_capital"] == 100_000
    assert meta["index_start_level"] == 2_500.0

    portfolio = read_json(repo / "data" / "portfolio.json")
    assert portfolio["cash"] == 100_000.0
    assert portfolio["positions"] == []

    daily = read_json(repo / "data" / "daily_values.json")
    assert daily[0]["portfolio_value"] == 100_000.0
    assert daily[0]["index_value"] == 100_000.0


def test_startkursen_fryses_och_skrivs_aldrig_om(repo, wire):
    wire()
    invoke(repo)
    wire(snap=snapshot(now_iso=MIDDAG, index=2_600.0))
    invoke(repo, now=MIDDAG)

    meta = read_json(repo / "data" / "meta.json")
    assert meta["index_start_level"] == 2_500.0

    # Index upp 4 % ska synas som 104 000 kr, inte som en ny startpunkt.
    daily = read_json(repo / "data" / "daily_values.json")
    assert daily[-1]["index_value"] == pytest.approx(104_000.0)


# -------------------------------------------------------------- affärer


def test_godkant_forslag_bokfors_med_motivering(repo, wire):
    wire(decision=fake_decision([buy_proposal()]))
    invoke(repo)

    portfolio = read_json(repo / "data" / "portfolio.json")
    assert len(portfolio["positions"]) == 1
    position = portfolio["positions"][0]
    assert position["ticker"] == ERIC
    assert position["stop_loss"] == 90.0
    assert position["rationale"] == "stigande trend och stark rapport"

    trades = read_json(repo / "data" / "trades.json")
    assert len(trades) == 1
    assert trades[0]["action"] == "buy"
    assert trades[0]["decision_id"] == "2026-11-23-morning"


def test_ogiltigt_forslag_loggas_men_bokfors_inte(repo, wire):
    # 50 000 kr i en enda aktie är över 20 %-taket.
    wire(decision=fake_decision([buy_proposal(amount=50_000)]))
    invoke(repo)

    assert read_json(repo / "data" / "portfolio.json")["positions"] == []
    assert read_json(repo / "data" / "trades.json") == []

    decisions = read_json(repo / "data" / "decisions.json")
    assert decisions[-1]["accepted"] == []
    assert decisions[-1]["rejected"][0]["reason"] == "over_positionstak"


def test_motiveringen_sparas_i_beslutsloggen(repo, wire):
    wire(decision=fake_decision([buy_proposal()], view="breda uppgangar"))
    invoke(repo)

    decisions = read_json(repo / "data" / "decisions.json")
    assert decisions[-1]["market_view"] == "breda uppgangar"
    assert decisions[-1]["model"] == "claude-sonnet-5-5"
    assert decisions[-1]["accepted"][0]["rationale"].startswith("stigande")


def test_stopp_stanger_positionen_i_nasta_runda(repo, wire):
    wire(decision=fake_decision([buy_proposal(stop=95.0)]))
    invoke(repo)
    assert read_json(repo / "data" / "portfolio.json")["positions"]

    # Nästa runda: kursen har fallit under stoppet.
    wire(snap=snapshot(now_iso=MIDDAG, eric=92.0), decision=fake_decision([]))
    invoke(repo, now=MIDDAG)

    portfolio = read_json(repo / "data" / "portfolio.json")
    assert portfolio["positions"] == []

    trades = read_json(repo / "data" / "trades.json")
    assert trades[-1]["reason"] == "stop_loss"
    assert trades[-1]["action"] == "sell"


def test_mal_stanger_positionen(repo, wire):
    wire(decision=fake_decision([buy_proposal(target=105.0)]))
    invoke(repo)

    wire(snap=snapshot(now_iso=MIDDAG, eric=108.0), decision=fake_decision([]))
    invoke(repo, now=MIDDAG)

    trades = read_json(repo / "data" / "trades.json")
    assert trades[-1]["reason"] == "take_profit"
    assert read_json(repo / "data" / "portfolio.json")["positions"] == []


# ------------------------------------------------------- avbrutna rundor


def test_trasig_datakalla_hoppar_over_rundan_utan_att_skriva(repo, wire):
    wire(data_error=DataUnavailable("yahoo svarade inte"))

    assert invoke(repo) == 0
    assert not (repo / "data" / "portfolio.json").exists()
    assert not (repo / "data" / "meta.json").exists()


def test_uteblivet_modellsvar_stoppar_inte_resten_av_rundan(repo, wire):
    wire(decision_error=DecisionUnavailable("API nere"))

    assert invoke(repo) == 0
    # Dagens värde sparas ändå, men ingen beslutspost skrivs.
    assert read_json(repo / "data" / "daily_values.json")
    assert read_json(repo / "data" / "decisions.json") == []


def test_helg_hoppar_over_rundan(repo, wire):
    wire()
    # Lördag 21 november 2026.
    assert invoke(repo, now="2026-11-21T08:15:00+00:00") == 0
    assert not (repo / "data" / "meta.json").exists()


def test_korning_utanfor_tidsfonstret_hoppas_over(repo, wire):
    wire()
    # 05:00 UTC är 06:00 svensk tid, långt före första sloten.
    assert invoke(repo, now="2026-11-23T05:00:00+00:00") == 0
    assert not (repo / "data" / "meta.json").exists()


def test_samma_slot_kors_inte_tva_ganger_samma_dag(repo, wire):
    wire(decision=fake_decision([buy_proposal()]))
    invoke(repo)
    assert len(read_json(repo / "data" / "trades.json")) == 1

    # Ett försenat jobb startar en gång till inom samma fönster.
    wire(snap=snapshot(now_iso="2026-11-23T08:45:00+00:00"),
         decision=fake_decision([buy_proposal(ticker=VOLV, stop=225.0, target=300.0)]))
    invoke(repo, now="2026-11-23T08:45:00+00:00")

    assert len(read_json(repo / "data" / "trades.json")) == 1


def test_force_kor_anda(repo, wire):
    wire(decision=fake_decision([buy_proposal()]))
    invoke(repo)

    wire(snap=snapshot(now_iso="2026-11-23T08:45:00+00:00"),
         decision=fake_decision([buy_proposal(ticker=VOLV, stop=225.0, target=300.0)]))
    invoke(repo, "--force", now="2026-11-23T08:45:00+00:00")

    assert len(read_json(repo / "data" / "trades.json")) == 2


def test_namngiven_slot_kors_oavsett_klockslag(repo, wire):
    wire()
    assert invoke(repo, "--slot", "close", now="2026-11-23T05:00:00+00:00") == 0
    assert read_json(repo / "data" / "meta.json")
    assert read_json(repo / "data" / "runs.json")["2026-11-23"] == ["close"]


def test_okand_slot_avvisas(repo, wire):
    wire()
    assert invoke(repo, "--slot", "lunch") == 0
    assert not (repo / "data" / "meta.json").exists()


def test_saknad_kurs_for_innehav_hoppar_over_rundan(repo, wire):
    wire(decision=fake_decision([buy_proposal()]))
    invoke(repo)

    # Nästa runda saknar ERIC helt. Portföljen går inte att värdera.
    utan_eric = MarketSnapshot(
        as_of=datetime.fromisoformat(MIDDAG),
        prices={VOLV: 250.0},
        history={VOLV: bars(date(2026, 11, 23), [240.0, 245.0, 248.0, 250.0])},
        benchmark_ticker=BENCH,
        benchmark_price=2_500.0,
        benchmark_history=tuple(bars(date(2026, 11, 23), [2_480.0, 2_500.0])),
    )
    wire(snap=utan_eric, decision=fake_decision([]))
    invoke(repo, now=MIDDAG)

    # Inget nytt dagligt värde, ingen ny affär.
    daily = read_json(repo / "data" / "daily_values.json")
    assert daily[-1]["slot"] == "morning"


# --------------------------------------------------------------- dry run


def test_dry_run_skriver_ingenting(repo, wire):
    wire(decision=fake_decision([buy_proposal()]))

    assert invoke(repo, "--dry-run") == 0

    for name in ("meta.json", "portfolio.json", "trades.json", "daily_values.json"):
        assert not (repo / "data" / name).exists()
    assert not (repo / "docs" / "data").exists()


def test_dry_run_lamnar_befintlig_state_orord(repo, wire):
    wire(decision=fake_decision([buy_proposal()]))
    invoke(repo)
    fore = (repo / "data" / "portfolio.json").read_text(encoding="utf-8")

    wire(snap=snapshot(now_iso=MIDDAG),
         decision=fake_decision([buy_proposal(ticker=VOLV, stop=225.0, target=300.0)]))
    invoke(repo, "--dry-run", now=MIDDAG)

    assert (repo / "data" / "portfolio.json").read_text(encoding="utf-8") == fore


# ------------------------------------------------------------ publicering


def test_sidans_underlag_publiceras_till_docs(repo, wire):
    wire(decision=fake_decision([buy_proposal()]))
    invoke(repo)

    summary = read_json(repo / "docs" / "data" / "summary.json")
    assert summary["total_value"] > 0
    assert summary["positions"][0]["ticker"] == ERIC
    assert summary["positions"][0]["rationale"]
    assert summary["performance"]["index_return_pct"] == 0.0
    assert "inte finansiell rådgivning" in summary["disclaimer"]

    for name in ("portfolio.json", "trades.json", "daily_values.json", "meta.json"):
        assert (repo / "docs" / "data" / name).exists()


def test_sammanfattningen_har_serie_for_grafen(repo, wire):
    wire()
    invoke(repo)
    wire(snap=snapshot(now_iso=MIDDAG, index=2_550.0))
    invoke(repo, now=MIDDAG)

    summary = read_json(repo / "data" / "summary.json")
    assert len(summary["series"]) == 1          # samma dag, en punkt
    assert summary["series"][0]["index"] == pytest.approx(102_000.0)
