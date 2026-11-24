"""Beslutslagret: promptbygge och tolkning av modellens svar.

Inget test rör API:et. En fejkad klient returnerar färdiga svarsobjekt.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aiaktier.decide import (
    RESPONSE_SCHEMA,
    DecisionContext,
    DecisionUnavailable,
    ask,
    build_system_prompt,
    build_user_prompt,
    summarize_history,
)
from aiaktier.market_data import Bar

from datetime import date, timedelta

ERIC = "ERIC-B.ST"


def make_bars(closes):
    start = date(2026, 8, 3)
    out = []
    cursor = start
    for close in closes:
        while cursor.weekday() >= 5:
            cursor += timedelta(days=1)
        out.append(Bar(cursor, close))
        cursor += timedelta(days=1)
    return out


def make_context(**overrides) -> DecisionContext:
    base = dict(
        slot="morning",
        local_time="2026-11-23T09:15:00+01:00",
        cash=100_000.0,
        total_value=100_000.0,
        start_capital=100_000.0,
        index_value=100_000.0,
        index_level=2_500.0,
        performance={"total_return_pct": 0.0, "index_return_pct": 0.0, "excess_pct": 0.0},
        positions=[],
        market={ERIC: summarize_history(make_bars([95.0, 97.0, 100.0]))},
        names={ERIC: "Ericsson B"},
        recent_trades=[],
        previous_rejections=[],
        max_position_pct=0.20,
        min_trade_sek=1_000.0,
        max_proposals=3,
        commission_pct=0.0025,
        commission_min_sek=1.0,
        spread_pct=0.0010,
    )
    base.update(overrides)
    return DecisionContext(**base)


class FakeMessages:
    def __init__(self, response, record=None):
        self.response = response
        self.record = record if record is not None else {}

    def create(self, **kwargs):
        self.record.update(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    """Efterliknar anthropic.Anthropic tillräckligt för att testa anropet."""

    def __init__(self, response, beta_response=None):
        self.record = {}
        self.beta_record = {}
        self.messages = FakeMessages(response, self.record)
        self.beta = SimpleNamespace(
            messages=FakeMessages(
                beta_response if beta_response is not None else response,
                self.beta_record,
            )
        )


def response_with(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=None,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=120, output_tokens=40),
    )


VALID = {
    "market_view": "breda uppgangar",
    "proposals": [{
        "action": "buy",
        "ticker": ERIC,
        "amount_sek": 15_000,
        "stop_loss": 90.0,
        "take_profit": 120.0,
        "rationale": "stark trend",
    }],
}


# ------------------------------------------------------------- statistik


def test_statistik_per_aktie():
    stats = summarize_history(make_bars([100.0, 102.0, 110.0]))

    assert stats["pris"] == 110.0
    assert stats["dag_pct"] == pytest.approx(7.84, abs=0.01)
    assert stats["hogsta_60d"] == 110.0
    assert stats["lagsta_60d"] == 100.0
    assert stats["fran_hogsta_pct"] == 0.0


def test_statistik_utan_historik_ar_tom():
    assert summarize_history([]) == {}


def test_saknade_perioder_blir_none():
    stats = summarize_history(make_bars([100.0, 105.0]))
    assert stats["dag_pct"] is not None
    assert stats["vecka_pct"] is None        # för kort historik


# ---------------------------------------------------------------- prompt


def test_systemprompten_innehaller_reglerna():
    prompt = build_system_prompt(make_context())

    assert "20 %" in prompt
    assert "stop_loss" in prompt
    assert "OMXS30" in prompt
    assert "Högst 3 förslag" in prompt
    # Modellen ska veta att den bara föreslår.
    assert "Koden avgör" in prompt


def test_systemprompten_raknar_ut_taket_i_kronor():
    # 20 % av 80 000 kr, så modellen slipper räkna själv.
    prompt = build_system_prompt(make_context(total_value=80_000.0))
    assert "cirka 16000 kr" in prompt


def test_anvandarprompten_innehaller_stallning_och_kurser():
    prompt = build_user_prompt(make_context())

    assert "Portföljvärde: 100000.00 kr" in prompt
    assert ERIC in prompt
    assert "Ericsson B" in prompt
    assert "Inga. Hela kapitalet ligger i kassa." in prompt


def test_oppna_positioner_visas_med_motivering():
    position = {
        "ticker": ERIC, "name": "Ericsson B", "shares": 100, "avg": 100.0,
        "price": 110.0, "value": 11_000.0, "weight": 11.0, "pnl": 1_000.0,
        "stop": 90.0, "target": 130.0, "why": "stark orderingang",
    }
    prompt = build_user_prompt(make_context(positions=[position]))

    assert "stark orderingang" in prompt
    assert "Stopp 90.00 / mål 130.00" in prompt


def test_avvisade_forslag_aterkopplas_till_modellen():
    rejections = [{
        "action": "buy", "ticker": ERIC,
        "reason": "over_positionstak", "detail": "30.0 % > 20.0 %",
    }]
    prompt = build_user_prompt(make_context(previous_rejections=rejections))

    assert "avvisades förra körningen" in prompt
    assert "over_positionstak" in prompt


def test_avslutade_affarer_visas():
    trades = [{
        "date": "2026-11-20", "action": "sell", "ticker": ERIC,
        "result": "+1200 kr", "reason": "take_profit", "why": "malet natt",
    }]
    prompt = build_user_prompt(make_context(recent_trades=trades))

    assert "+1200 kr" in prompt
    assert "take_profit" in prompt


# ----------------------------------------------------------- anropet


def test_giltigt_svar_tolkas():
    client = FakeClient(response_with(VALID))
    decision = ask(make_context(), {"name": "claude-sonnet-5-5"}, client=client)

    assert decision.market_view == "breda uppgangar"
    assert len(decision.proposals) == 1
    assert decision.proposals[0]["ticker"] == ERIC
    assert decision.usage["input_tokens"] == 120


def test_anropet_tvingar_fram_schemat():
    client = FakeClient(response_with(VALID))
    ask(make_context(), {"name": "claude-sonnet-5-5", "effort": "low"}, client=client)

    sent = client.beta_record
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["output_config"]["format"]["schema"] == RESPONSE_SCHEMA
    assert sent["output_config"]["effort"] == "low"
    # Ingen thinking-parameter: adaptiv tänkning är standard på modellen.
    assert "thinking" not in sent


def test_tom_forslagslista_ar_ett_giltigt_svar():
    client = FakeClient(response_with({"market_view": "avvaktar", "proposals": []}))
    decision = ask(make_context(), {}, client=client)

    assert decision.proposals == []
    assert decision.market_view == "avvaktar"


def test_serverside_fallback_kan_stangas_av():
    client = FakeClient(response_with(VALID))
    ask(make_context(), {"server_side_fallback": False}, client=client)

    assert client.beta_record == {}
    assert client.record["model"]


def test_avbojt_svar_blir_dataunavailable():
    refusal = response_with({}, stop_reason="refusal")
    refusal.stop_details = SimpleNamespace(category="general_harms")
    client = FakeClient(refusal)

    with pytest.raises(DecisionUnavailable) as exc:
        ask(make_context(), {}, client=client)
    assert "avbojde" in str(exc.value)


def test_kapat_svar_blir_dataunavailable():
    client = FakeClient(response_with(VALID, stop_reason="max_tokens"))

    with pytest.raises(DecisionUnavailable) as exc:
        ask(make_context(), {}, client=client)
    assert "max_tokens" in str(exc.value)


def test_trasig_json_blir_dataunavailable():
    client = FakeClient(response_with("{det har ar inte json"))

    with pytest.raises(DecisionUnavailable) as exc:
        ask(make_context(), {}, client=client)
    assert "JSON" in str(exc.value)


def test_tomt_svar_blir_dataunavailable():
    client = FakeClient(response_with(""))

    with pytest.raises(DecisionUnavailable):
        ask(make_context(), {}, client=client)


def test_proposals_som_inte_ar_lista_avvisas():
    client = FakeClient(response_with({"market_view": "x", "proposals": "koep allt"}))

    with pytest.raises(DecisionUnavailable) as exc:
        ask(make_context(), {}, client=client)
    assert "lista" in str(exc.value)


def test_natverksfel_blir_dataunavailable():
    import anthropic

    client = FakeClient(anthropic.APIConnectionError(request=None))

    with pytest.raises(DecisionUnavailable) as exc:
        ask(make_context(), {}, client=client)
    assert "API" in str(exc.value)


# ------------------------------------------- serverside-fallback och 400-svar


def bad_request(message):
    """Ett riktigt anthropic.BadRequestError, byggt som SDK:n själv bygger det."""
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, request=request)
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return anthropic.BadRequestError(message, response=response, body=body)


CREDIT = "Your credit balance is too low to access the Anthropic API."


def test_kreditfel_skylls_inte_pa_fallback_flaggan(caplog):
    # Exakt det som hände på GitHub den 24 november.
    client = FakeClient(bad_request(CREDIT), beta_response=bad_request(CREDIT))

    with caplog.at_level("WARNING"):
        with pytest.raises(DecisionUnavailable) as exc:
            ask(make_context(), {}, client=client)

    loggen = caplog.text
    assert "accepterades inte" not in loggen
    assert "felet ligger inte i fallback-flaggan" in loggen
    assert "flaggan var orsaken" not in loggen
    # Det verkliga felet når fram, och utan hela svarsdumpen runt omkring.
    assert "credit balance is too low" in str(exc.value)
    assert "invalid_request_error" not in str(exc.value)


def test_avvisad_flagga_konstateras_forst_nar_vanligt_anrop_gar_igenom(caplog):
    client = FakeClient(
        response_with(VALID),
        beta_response=bad_request("fallbacks: Extra inputs are not permitted"),
    )

    with caplog.at_level("WARNING"):
        decision = ask(make_context(), {}, client=client)

    assert decision.proposals[0]["ticker"] == ERIC
    assert "flaggan var orsaken" in caplog.text
    assert "server_side_fallback: false" in caplog.text


def test_lyckat_anrop_med_fallback_gor_bara_ett_anrop(caplog):
    client = FakeClient(bad_request("ska aldrig anropas"), beta_response=response_with(VALID))

    with caplog.at_level("WARNING"):
        ask(make_context(), {}, client=client)

    assert client.record == {}            # det vanliga anropet gjordes aldrig
    assert caplog.text == ""


def test_utan_fallback_gors_inget_extra_anrop_vid_400():
    client = FakeClient(bad_request(CREDIT), beta_response=response_with(VALID))

    with pytest.raises(DecisionUnavailable):
        ask(make_context(), {"server_side_fallback": False}, client=client)

    assert client.beta_record == {}
