"""Bokföringen: köp, sälj, stopp, mål, courtage och otillräcklig kassa."""

from __future__ import annotations

import pytest

from aiaktier.portfolio import (
    REASON_DECISION,
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
    BookkeepingError,
    InsufficientCash,
    InsufficientShares,
    NoSuchPosition,
    Portfolio,
)

from conftest import TS

ERIC = "ERIC-B.ST"
VOLV = "VOLV-B.ST"


# ---------------------------------------------------------------- köp


def test_kop_drar_kassan_och_oppnar_position(portfolio, fees):
    trade = portfolio.buy(
        ERIC, 20_000, 100.0, stop_loss=90.0, take_profit=120.0,
        fees=fees, timestamp=TS, rationale="test", decision_id="d1",
    )

    assert trade.action == "buy"
    assert trade.shares == 199           # 200 hade spräckt budgeten
    assert trade.price == pytest.approx(100.05, abs=1e-6)
    assert trade.commission == pytest.approx(49.77, abs=0.01)
    assert trade.cash_delta == pytest.approx(-19_959.72, abs=0.02)
    assert trade.reason == REASON_DECISION
    assert trade.rationale == "test"
    assert trade.decision_id == "d1"

    assert portfolio.cash == pytest.approx(80_040.28, abs=0.02)
    pos = portfolio.positions[ERIC]
    assert pos.shares == 199
    assert pos.stop_loss == pytest.approx(90.0)
    assert pos.take_profit == pytest.approx(120.0)
    assert pos.rationale == "test"


def test_kop_ger_bara_hela_aktier(portfolio, fees):
    trade = portfolio.buy(
        ERIC, 1_000, 137.0, 120.0, 160.0, fees, TS
    )
    assert isinstance(trade.shares, int)
    assert trade.shares == 7             # 7 x 137,0685 = 959,48 kr
    assert portfolio.cash < 100_000


def test_kop_begransas_av_kassan_inte_av_onskat_belopp(fees):
    p = Portfolio.new(5_000)
    trade = p.buy(ERIC, 50_000, 100.0, 90.0, 120.0, fees, TS)
    # Önskat belopp var 50 000, men bara 5 000 fanns.
    assert trade.shares == 49
    assert p.cash >= 0


def test_pafyllning_slar_ihop_anskaffningsvardet_och_skriver_om_nivaer(portfolio, fees):
    portfolio.buy(ERIC, 10_000, 100.0, 90.0, 120.0, fees, TS, decision_id="d1")
    first = portfolio.positions[ERIC]
    portfolio.buy(ERIC, 10_000, 110.0, 99.0, 130.0, fees, TS, decision_id="d2")
    second = portfolio.positions[ERIC]

    assert second.shares > first.shares
    assert second.cost_basis > first.cost_basis
    # Senaste beslutets nivåer är de som gäller.
    assert second.stop_loss == pytest.approx(99.0)
    assert second.take_profit == pytest.approx(130.0)
    assert second.decision_id == "d2"


def test_avgifter_gor_kopet_dyrare_an_rena_aktiepriset(portfolio, free_fees, fees):
    utan = Portfolio.new(100_000)
    utan.buy(ERIC, 20_000, 100.0, 90.0, 120.0, free_fees, TS)
    med = Portfolio.new(100_000)
    med.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)

    # Samma budget, men spread och courtage äter upp aktier eller kassa.
    assert med.positions[ERIC].cost_basis > 0
    assert med.cash < utan.cash or med.positions[ERIC].shares < utan.positions[ERIC].shares


# ---------------------------------------------------- otillräcklig kassa


def test_otillracklig_kassa_kastar_fel_och_lamnar_portfoljen_orord(fees):
    p = Portfolio.new(50.0)
    with pytest.raises(InsufficientCash):
        p.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)

    assert p.cash == pytest.approx(50.0)
    assert p.positions == {}
    assert p.trade_seq == 0


def test_tom_kassa_kan_inte_kopa(fees):
    p = Portfolio.new(0.0)
    with pytest.raises(InsufficientCash):
        p.buy(ERIC, 1_000, 100.0, 90.0, 120.0, fees, TS)


def test_negativt_belopp_avvisas(portfolio, fees):
    with pytest.raises(BookkeepingError):
        portfolio.buy(ERIC, -1_000, 100.0, 90.0, 120.0, fees, TS)


def test_kassan_blir_aldrig_negativ_av_en_rad_kop(portfolio, fees):
    for mid in (100.0, 212.5, 88.25, 341.0, 19.9):
        try:
            portfolio.buy(ERIC, 30_000, mid, mid * 0.9, mid * 1.2, fees, TS)
        except InsufficientCash:
            pass
        assert portfolio.cash >= 0


# --------------------------------------------------------------- sälj


def test_salj_hela_innehavet_bokfor_vinst(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS, rationale="varfor")
    trade = portfolio.sell(ERIC, 199, 120.0, fees, TS)

    assert trade.action == "sell"
    assert trade.shares == 199
    assert trade.price == pytest.approx(119.94, abs=1e-6)
    assert trade.commission == pytest.approx(59.67, abs=0.01)
    assert trade.realized_pnl == pytest.approx(3_848.67, abs=0.05)
    # Motiveringen från köpet följer med in i den stängda affären.
    assert trade.rationale == "varfor"

    assert ERIC not in portfolio.positions
    assert portfolio.cash == pytest.approx(103_848.67, abs=0.05)
    assert portfolio.realized_pnl == pytest.approx(3_848.67, abs=0.05)


def test_salj_med_forlust_bokfors_negativt(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    trade = portfolio.sell(ERIC, 199, 80.0, fees, TS)

    assert trade.realized_pnl < 0
    assert portfolio.realized_pnl < 0
    assert portfolio.cash < 100_000


def test_delsalj_minskar_innehav_och_anskaffningsvarde_proportionellt(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    before = portfolio.positions[ERIC]

    portfolio.sell(ERIC, 100, 125.0, fees, TS)
    after = portfolio.positions[ERIC]

    assert after.shares == before.shares - 100
    # Snittpriset påverkas inte av ett delsälj, bara totalen.
    assert after.avg_price == pytest.approx(before.avg_price, abs=0.01)
    assert after.cost_basis == pytest.approx(
        before.cost_basis * after.shares / before.shares, abs=0.05
    )
    assert after.stop_loss == pytest.approx(before.stop_loss)


def test_salj_utan_innehav_kastar_fel(portfolio, fees):
    with pytest.raises(NoSuchPosition):
        portfolio.sell(VOLV, 10, 100.0, fees, TS)
    with pytest.raises(NoSuchPosition):
        portfolio.close(VOLV, 100.0, fees, TS)


def test_salj_fler_aktier_an_innehavet_kastar_fel(portfolio, fees):
    portfolio.buy(ERIC, 10_000, 100.0, 90.0, 120.0, fees, TS)
    held = portfolio.positions[ERIC].shares
    with pytest.raises(InsufficientShares):
        portfolio.sell(ERIC, held + 1, 100.0, fees, TS)
    # Innehavet ska vara orört efter det avvisade försöket.
    assert portfolio.positions[ERIC].shares == held


def test_salj_noll_aktier_avvisas(portfolio, fees):
    portfolio.buy(ERIC, 10_000, 100.0, 90.0, 120.0, fees, TS)
    with pytest.raises(BookkeepingError):
        portfolio.sell(ERIC, 0, 100.0, fees, TS)


# ------------------------------------------------------- stopp och mål


def test_stangning_pa_stopp_bokfors_med_orsak_stop_loss(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS, rationale="tes")
    trade = portfolio.close(ERIC, 89.0, fees, TS, reason=REASON_STOP_LOSS)

    assert trade.reason == REASON_STOP_LOSS
    assert trade.action == "sell"
    assert trade.shares == 199
    assert trade.realized_pnl < 0
    assert ERIC not in portfolio.positions


def test_stangning_pa_mal_bokfors_med_orsak_take_profit(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    trade = portfolio.close(ERIC, 121.0, fees, TS, reason=REASON_TAKE_PROFIT)

    assert trade.reason == REASON_TAKE_PROFIT
    assert trade.realized_pnl > 0
    assert portfolio.positions == {}


# ----------------------------------------------------------- värdering


def test_totalt_varde_ar_kassa_plus_innehav(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    prices = {ERIC: 110.0}

    assert portfolio.market_value(prices) == pytest.approx(199 * 110.0, abs=0.01)
    assert portfolio.total_value(prices) == pytest.approx(
        portfolio.cash + 199 * 110.0, abs=0.01
    )


def test_saknat_pris_for_innehav_kastar_keyerror(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    with pytest.raises(KeyError):
        portfolio.market_value({})


def test_positionsvikt_rakans_pa_totalt_varde(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    prices = {ERIC: 100.0}
    weight = portfolio.position_weight(ERIC, prices)

    assert 0.19 < weight < 0.21
    assert portfolio.position_weight(VOLV, prices) == 0.0


def test_orealiserad_vinst_rakans_mot_anskaffningsvardet(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    pos = portfolio.positions[ERIC]

    assert pos.unrealized_pnl(100.0) < 0        # spread och courtage kostar direkt
    assert pos.unrealized_pnl(120.0) > 0


# ------------------------------------------------------- serialisering


def test_portfoljen_overlever_en_runda_till_json_och_tillbaka(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS, rationale="m", decision_id="d")
    portfolio.buy(VOLV, 15_000, 250.0, 225.0, 300.0, fees, TS, rationale="n", decision_id="d")
    portfolio.sell(ERIC, 50, 110.0, fees, TS)

    restored = Portfolio.from_dict(portfolio.to_dict())

    assert restored.cash == pytest.approx(portfolio.cash, abs=0.01)
    assert restored.realized_pnl == pytest.approx(portfolio.realized_pnl, abs=0.01)
    assert restored.trade_seq == portfolio.trade_seq
    assert set(restored.positions) == set(portfolio.positions)
    for ticker, pos in portfolio.positions.items():
        other = restored.positions[ticker]
        assert other.shares == pos.shares
        assert other.cost_basis == pytest.approx(pos.cost_basis, abs=0.01)
        assert other.rationale == pos.rationale


def test_affars_id_ar_unika_inom_en_korning(portfolio, fees):
    ids = []
    for _ in range(3):
        trade = portfolio.buy(ERIC, 5_000, 100.0, 90.0, 120.0, fees, TS)
        ids.append(trade.trade_id)
    assert len(set(ids)) == 3
