"""Valideraren: alla handelsregler som koden ska upprätthålla."""

from __future__ import annotations

import pytest

from aiaktier.portfolio import Portfolio
from aiaktier.validator import (
    REJ_BAD_AMOUNT,
    REJ_BELOW_MIN_TRADE,
    REJ_ILLOGICAL_LEVELS,
    REJ_INSUFFICIENT_CASH,
    REJ_MALFORMED,
    REJ_MISSING_LEVELS,
    REJ_NO_POSITION,
    REJ_NO_PRICE,
    REJ_NOT_IN_UNIVERSE,
    REJ_POSITION_CAP,
    REJ_TOO_MANY,
    REJ_UNKNOWN_ACTION,
    ProposalError,
    coerce_proposal,
    shares_to_sell,
    validate_proposals,
)

from conftest import TS

ERIC = "ERIC-B.ST"
VOLV = "VOLV-B.ST"
ABB = "ABB.ST"
AZN = "AZN.ST"

PRICES = {ERIC: 100.0, VOLV: 250.0, ABB: 500.0, AZN: 1_200.0}


def buy(ticker=ERIC, amount=15_000, stop=90.0, target=120.0, rationale="skal"):
    return {
        "action": "buy",
        "ticker": ticker,
        "amount_sek": amount,
        "stop_loss": stop,
        "take_profit": target,
        "rationale": rationale,
    }


def run(proposals, portfolio, fees, universe, **kwargs):
    options = {
        "portfolio": portfolio,
        "prices": PRICES,
        "universe": universe,
        "fees": fees,
        "max_position_pct": 0.20,
        "min_trade_sek": 1_000.0,
        "max_proposals": 3,
    }
    options.update(kwargs)
    return validate_proposals(proposals, **options)


def reasons(result):
    return [r.reason for r in result.rejected]


# ------------------------------------------------------------ tolkning


def test_giltigt_forslag_tolkas():
    p = coerce_proposal(buy())
    assert p.action == "buy"
    assert p.ticker == ERIC
    assert p.amount_sek == 15_000.0
    assert p.stop_loss == 90.0
    assert p.rationale == "skal"


def test_ticker_normaliseras_till_versaler():
    assert coerce_proposal(buy(ticker="eric-b.st")).ticker == ERIC


def test_strangar_som_tal_accepteras():
    raw = buy()
    raw["amount_sek"] = "15000"
    raw["stop_loss"] = "90.5"
    p = coerce_proposal(raw)
    assert p.amount_sek == 15_000.0
    assert p.stop_loss == 90.5


def test_otolkbara_forslag_kastar_fel():
    with pytest.raises(ProposalError):
        coerce_proposal("inte ett objekt")
    with pytest.raises(ProposalError):
        coerce_proposal({"action": "buy", "amount_sek": 1000})          # ticker saknas
    with pytest.raises(ProposalError):
        coerce_proposal({"action": "buy", "ticker": ERIC})              # belopp saknas
    with pytest.raises(ProposalError):
        coerce_proposal({"action": "buy", "ticker": ERIC, "amount_sek": "mycket"})


def test_trasiga_forslag_avvisas_men_stoppar_inte_de_ovriga(portfolio, fees, universe):
    result = run(["skrap", buy(), {"action": "buy"}], portfolio, fees, universe)

    assert len(result.accepted) == 1
    assert reasons(result) == [REJ_MALFORMED, REJ_MALFORMED]


# ------------------------------------------------------------ universum


def test_ticker_utanfor_universumet_avvisas(portfolio, fees, universe):
    result = run([buy(ticker="TSLA")], portfolio, fees, universe)

    assert result.accepted == ()
    assert reasons(result) == [REJ_NOT_IN_UNIVERSE]


def test_ticker_utan_pris_avvisas(portfolio, fees):
    result = run([buy(ticker="SAAB-B.ST")], portfolio, fees, list(PRICES) + ["SAAB-B.ST"])

    assert reasons(result) == [REJ_NO_PRICE]


def test_okand_atgard_avvisas(portfolio, fees, universe):
    raw = buy()
    raw["action"] = "hold"
    result = run([raw], portfolio, fees, universe)

    assert reasons(result) == [REJ_UNKNOWN_ACTION]


# ------------------------------------------------------- stopp och mål


def test_saknat_stopp_eller_mal_avvisas(portfolio, fees, universe):
    utan_stopp = buy()
    utan_stopp["stop_loss"] = None
    utan_mal = buy()
    del utan_mal["take_profit"]

    result = run([utan_stopp, utan_mal], portfolio, fees, universe)

    assert result.accepted == ()
    assert reasons(result) == [REJ_MISSING_LEVELS, REJ_MISSING_LEVELS]


def test_orimliga_nivaer_avvisas(portfolio, fees, universe):
    omkastade = buy(stop=120.0, target=90.0)
    bada_over = buy(stop=110.0, target=130.0)
    bada_under = buy(stop=70.0, target=95.0)

    result = run([omkastade, bada_over, bada_under], portfolio, fees, universe)

    assert result.accepted == ()
    assert reasons(result) == [REJ_ILLOGICAL_LEVELS] * 3


def test_salj_kraver_inga_nivaer(portfolio, fees, universe):
    portfolio.buy(ERIC, 15_000, 100.0, 90.0, 120.0, fees, TS)
    result = run(
        [{"action": "sell", "ticker": ERIC, "amount_sek": 15_000, "rationale": "ut"}],
        portfolio, fees, universe,
    )

    assert len(result.accepted) == 1
    assert result.rejected == ()


# ------------------------------------------------------------- belopp


def test_negativt_eller_nollbelopp_avvisas(portfolio, fees, universe):
    result = run([buy(amount=0), buy(amount=-5_000)], portfolio, fees, universe)
    assert reasons(result) == [REJ_BAD_AMOUNT, REJ_BAD_AMOUNT]


def test_for_liten_affar_avvisas(portfolio, fees, universe):
    result = run([buy(amount=500)], portfolio, fees, universe)
    assert reasons(result) == [REJ_BELOW_MIN_TRADE]


def test_litet_salj_tillats_om_det_stanger_hela_innehavet(portfolio, fees, universe):
    # Ett innehav värt under minsta affär ska gå att bli av med.
    portfolio.buy(AZN, 2_400, 1_200.0, 1_000.0, 1_500.0, fees, TS)
    assert portfolio.positions[AZN].shares == 1

    result = run(
        [{"action": "sell", "ticker": AZN, "amount_sek": 900, "rationale": "ut"}],
        portfolio, fees, universe,
    )
    assert reasons(result) == [REJ_BELOW_MIN_TRADE]

    result = run(
        [{"action": "sell", "ticker": AZN, "amount_sek": 1_200, "rationale": "ut"}],
        portfolio, fees, universe,
    )
    assert len(result.accepted) == 1


# -------------------------------------------------------------- kassa


def test_belopp_over_kassan_avvisas(fees, universe):
    p = Portfolio.new(10_000)
    result = run([buy(amount=50_000)], p, fees, universe)

    assert reasons(result) == [REJ_INSUFFICIENT_CASH]


def test_kassan_racker_inte_till_en_hel_aktie(fees, universe):
    p = Portfolio.new(1_100)
    result = run(
        [buy(ticker=AZN, amount=1_050, stop=1_000.0, target=1_500.0)],
        p, fees, universe,
    )

    assert reasons(result) == [REJ_INSUFFICIENT_CASH]


def test_tva_kop_kan_inte_gora_ansprak_pa_samma_kassa(fees, universe):
    p = Portfolio.new(20_000)
    # Positionstaket lyfts bort, så att det är kassan som prövas och inget annat.
    result = run(
        [buy(ticker=ERIC, amount=15_000),
         buy(ticker=VOLV, amount=15_000, stop=225.0, target=300.0)],
        p, fees, universe, max_position_pct=1.0,
    )

    assert len(result.accepted) == 1
    assert result.accepted[0].ticker == ERIC
    assert reasons(result) == [REJ_INSUFFICIENT_CASH]


# -------------------------------------------------------- positionstak


def test_kop_exakt_pa_taket_slapps_igenom(portfolio, fees, universe):
    # 20 % av 100 000 kr. Spread och courtage gör den bokförda positionen
    # marginellt mindre, så den ligger precis innanför taket.
    result = run([buy(amount=20_000)], portfolio, fees, universe)

    assert len(result.accepted) == 1
    assert result.rejected == ()


def test_kop_over_taket_avvisas(portfolio, fees, universe):
    result = run([buy(amount=30_000)], portfolio, fees, universe)

    assert result.accepted == ()
    assert reasons(result) == [REJ_POSITION_CAP]


def test_pafyllning_som_spracker_taket_avvisas(portfolio, fees, universe):
    portfolio.buy(ERIC, 18_000, 100.0, 90.0, 120.0, fees, TS)
    # Innehavet är redan nära taket; ytterligare 10 000 kr spräcker det.
    result = run([buy(ticker=ERIC, amount=10_000)], portfolio, fees, universe)

    assert reasons(result) == [REJ_POSITION_CAP]


def test_taket_galler_per_aktie_inte_for_portfoljen(portfolio, fees, universe):
    # Fyra olika aktier om vardera knappt 20 % ska alla gå igenom.
    result = run(
        [buy(ticker=ERIC, amount=18_000),
         buy(ticker=VOLV, amount=18_000, stop=225.0, target=300.0),
         buy(ticker=ABB, amount=18_000, stop=450.0, target=600.0)],
        portfolio, fees, universe,
    )

    assert len(result.accepted) == 3
    assert result.rejected == ()


def test_strangare_tak_kan_konfigureras(portfolio, fees, universe):
    result = run([buy(amount=15_000)], portfolio, fees, universe, max_position_pct=0.10)
    assert reasons(result) == [REJ_POSITION_CAP]


# --------------------------------------------------------------- sälj


def test_salj_utan_innehav_avvisas(portfolio, fees, universe):
    result = run(
        [{"action": "sell", "ticker": VOLV, "amount_sek": 10_000, "rationale": "ut"}],
        portfolio, fees, universe,
    )

    assert reasons(result) == [REJ_NO_POSITION]


def test_salj_kan_inte_upprepas_pa_samma_innehav(portfolio, fees, universe):
    portfolio.buy(ERIC, 15_000, 100.0, 90.0, 120.0, fees, TS)
    sell = {"action": "sell", "ticker": ERIC, "amount_sek": 15_000, "rationale": "ut"}

    result = run([dict(sell), dict(sell)], portfolio, fees, universe)

    assert len(result.accepted) == 1
    assert reasons(result) == [REJ_NO_POSITION]


def test_shares_to_sell_begransas_av_innehavet():
    assert shares_to_sell(10_000, 100.0, 50) == 50      # beloppet täcker allt
    assert shares_to_sell(2_000, 100.0, 50) == 20
    assert shares_to_sell(50, 100.0, 50) == 0           # inte ens en aktie
    assert shares_to_sell(1_000, 0.0, 50) == 0
    assert shares_to_sell(1_000, 100.0, 0) == 0


def test_belopp_som_nastan_tacker_innehavet_stanger_positionen():
    # 4 975 kr mot ett innehav värt 5 000 kr: stäng hela, lämna inte en rest.
    assert shares_to_sell(4_975, 100.0, 50) == 50


# ------------------------------------------------------ tak för antal


def test_fler_forslag_an_taket_avvisas(portfolio, fees, universe):
    result = run(
        [buy(ticker=ERIC, amount=10_000),
         buy(ticker=VOLV, amount=10_000, stop=225.0, target=300.0),
         buy(ticker=ABB, amount=10_000, stop=450.0, target=600.0),
         buy(ticker=AZN, amount=10_000, stop=1_000.0, target=1_500.0)],
        portfolio, fees, universe,
    )

    assert len(result.accepted) == 3
    assert reasons(result) == [REJ_TOO_MANY]


# ------------------------------------------------- portföljen lämnas orörd


def test_valideraren_ror_aldrig_den_riktiga_portfoljen(portfolio, fees, universe):
    before = portfolio.to_dict()
    run([buy(), buy(ticker=VOLV, stop=225.0, target=300.0)], portfolio, fees, universe)

    assert portfolio.to_dict() == before


def test_resultatet_gar_att_serialisera(portfolio, fees, universe):
    result = run([buy(), buy(ticker="TSLA")], portfolio, fees, universe)
    d = result.to_dict()

    assert len(d["accepted"]) == 1
    assert d["rejected"][0]["reason"] == REJ_NOT_IN_UNIVERSE
    assert "detail" in d["rejected"][0]
