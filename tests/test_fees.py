"""Courtage och spread."""

from __future__ import annotations

import pytest

from aiaktier.fees import FeeModel


def test_spread_laggs_pa_vid_kop_och_dras_av_vid_salj(fees):
    assert fees.buy_fill_price(100.0) == pytest.approx(100.05, abs=1e-6)
    assert fees.sell_fill_price(100.0) == pytest.approx(99.95, abs=1e-6)
    # Köparen betalar alltid mer än säljaren får. Det är hela poängen med spread.
    assert fees.buy_fill_price(250.0) > fees.sell_fill_price(250.0)


def test_courtage_ar_procent_pa_stora_affarer(fees):
    assert fees.commission(10_000.0) == pytest.approx(25.0, abs=0.01)
    assert fees.commission(100_000.0) == pytest.approx(250.0, abs=0.01)


def test_minimicourtage_galler_pa_sma_affarer(fees):
    # 0,25 % av 100 kr är 25 öre, men minimiavgiften är 1 kr.
    assert fees.commission(100.0) == pytest.approx(1.0, abs=0.01)
    assert fees.commission(1.0) == pytest.approx(1.0, abs=0.01)


def test_inget_courtage_pa_noll(fees):
    assert fees.commission(0.0) == 0.0


def test_kopkostnad_ar_belopp_plus_courtage(fees):
    # 100 aktier à mid 100 fylls till 100,05 = 10 005 kr, courtage 25,01 kr.
    cost = fees.buy_cost(100, 100.0)
    assert cost == pytest.approx(10_005.0 + 25.01, abs=0.02)


def test_max_shares_ryms_inom_budgeten(fees):
    shares = fees.max_shares_for_budget(10_000.0, 100.0)
    assert shares == 99
    assert fees.buy_cost(shares, 100.0) <= 10_000.0
    # Och en aktie till hade spräckt budgeten.
    assert fees.buy_cost(shares + 1, 100.0) > 10_000.0


def test_max_shares_ar_noll_nar_budgeten_inte_racker_till_en_aktie(fees):
    assert fees.max_shares_for_budget(50.0, 100.0) == 0
    assert fees.max_shares_for_budget(0.0, 100.0) == 0
    assert fees.max_shares_for_budget(-100.0, 100.0) == 0


def test_minimicourtaget_ater_upp_sista_aktien(fees):
    # Budget 101 kr, aktie till 100,05 kr: 100,05 + 1 kr courtage = 101,05 kr.
    # Det ryms inte, så svaret ska vara noll aktier och inte en.
    assert fees.max_shares_for_budget(101.0, 100.0) == 0
    assert fees.max_shares_for_budget(102.0, 100.0) == 1


def test_negativa_avgifter_avvisas():
    with pytest.raises(ValueError):
        FeeModel(commission_pct=-0.01)
    with pytest.raises(ValueError):
        FeeModel(spread_pct=-0.01)


def test_ogiltigt_mid_pris_avvisas(fees):
    with pytest.raises(ValueError):
        fees.buy_fill_price(0.0)
    with pytest.raises(ValueError):
        fees.sell_fill_price(-10.0)
