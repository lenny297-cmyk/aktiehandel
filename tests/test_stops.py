"""Stopp och mål: träff, ingen träff, saknat pris och bokföring av träffen."""

from __future__ import annotations

import pytest

from aiaktier.portfolio import REASON_STOP_LOSS, REASON_TAKE_PROFIT
from aiaktier.stops import apply_hits, check_levels, levels_are_sane

from conftest import TS

ERIC = "ERIC-B.ST"
VOLV = "VOLV-B.ST"


@pytest.fixture
def med_position(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, stop_loss=90.0, take_profit=120.0,
                  fees=fees, timestamp=TS, rationale="motivering")
    return portfolio


def test_ingen_traff_inom_intervallet(med_position):
    assert check_levels(med_position, {ERIC: 105.0}) == []
    assert check_levels(med_position, {ERIC: 90.01}) == []
    assert check_levels(med_position, {ERIC: 119.99}) == []


def test_stopp_traffar_pa_och_under_nivan(med_position):
    exakt = check_levels(med_position, {ERIC: 90.0})
    under = check_levels(med_position, {ERIC: 85.0})

    assert [h.reason for h in exakt] == [REASON_STOP_LOSS]
    assert [h.reason for h in under] == [REASON_STOP_LOSS]
    assert under[0].level == pytest.approx(90.0)
    assert under[0].price == pytest.approx(85.0)


def test_mal_traffar_pa_och_over_nivan(med_position):
    exakt = check_levels(med_position, {ERIC: 120.0})
    over = check_levels(med_position, {ERIC: 130.0})

    assert [h.reason for h in exakt] == [REASON_TAKE_PROFIT]
    assert [h.reason for h in over] == [REASON_TAKE_PROFIT]
    assert over[0].level == pytest.approx(120.0)


def test_saknat_pris_lamnar_positionen_i_fred(med_position):
    # Ingen träff, ingen krasch, ingen gissning.
    assert check_levels(med_position, {}) == []
    assert ERIC in med_position.positions


def test_flera_positioner_kontrolleras_var_for_sig(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    portfolio.buy(VOLV, 20_000, 250.0, 225.0, 300.0, fees, TS)

    hits = check_levels(portfolio, {ERIC: 85.0, VOLV: 310.0})
    by_ticker = {h.ticker: h.reason for h in hits}

    assert by_ticker == {ERIC: REASON_STOP_LOSS, VOLV: REASON_TAKE_PROFIT}


def test_stoppet_vinner_om_bada_nivaerna_brots(portfolio, fees):
    # En position med stopp över målet är orimlig och ska aldrig passera
    # valideraren, men om den ändå finns ska stoppet gälla.
    portfolio.buy(ERIC, 20_000, 100.0, stop_loss=120.0, take_profit=90.0,
                  fees=fees, timestamp=TS)
    hits = check_levels(portfolio, {ERIC: 100.0})

    assert [h.reason for h in hits] == [REASON_STOP_LOSS]


def test_nivaer_pa_noll_ignoreras(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, stop_loss=0.0, take_profit=0.0,
                  fees=fees, timestamp=TS)
    assert check_levels(portfolio, {ERIC: 1.0}) == []


# ------------------------------------------------- bokföring av träffen


def test_apply_hits_stanger_positionen_med_ratt_orsak(med_position, fees):
    hits = check_levels(med_position, {ERIC: 85.0})
    trades = apply_hits(med_position, hits, {ERIC: 85.0}, fees, TS)

    assert len(trades) == 1
    assert trades[0].reason == REASON_STOP_LOSS
    assert trades[0].action == "sell"
    assert trades[0].realized_pnl < 0
    assert ERIC not in med_position.positions
    # Motiveringen från köpet följer med den stängda affären.
    assert trades[0].rationale == "motivering"


def test_apply_hits_pa_tom_lista_gor_ingenting(med_position, fees):
    trades = apply_hits(med_position, [], {ERIC: 105.0}, fees, TS)
    assert trades == []
    assert ERIC in med_position.positions


def test_apply_hits_stanger_flera_positioner(portfolio, fees):
    portfolio.buy(ERIC, 20_000, 100.0, 90.0, 120.0, fees, TS)
    portfolio.buy(VOLV, 20_000, 250.0, 225.0, 300.0, fees, TS)
    prices = {ERIC: 85.0, VOLV: 310.0}

    trades = apply_hits(portfolio, check_levels(portfolio, prices), prices, fees, TS)

    assert len(trades) == 2
    assert portfolio.positions == {}


# ------------------------------------------------------ rimliga nivåer


def test_rimliga_nivaer_kraver_stopp_under_och_mal_over_priset():
    assert levels_are_sane(100.0, 90.0, 120.0)
    assert not levels_are_sane(100.0, 120.0, 90.0)     # omkastade
    assert not levels_are_sane(100.0, 100.0, 120.0)    # stopp på priset
    assert not levels_are_sane(100.0, 90.0, 100.0)     # mål på priset
    assert not levels_are_sane(100.0, 110.0, 120.0)    # båda över priset


def test_saknade_eller_nollade_nivaer_ar_inte_rimliga():
    assert not levels_are_sane(100.0, None, 120.0)
    assert not levels_are_sane(100.0, 90.0, None)
    assert not levels_are_sane(100.0, 0.0, 120.0)
    assert not levels_are_sane(100.0, 90.0, 0.0)
