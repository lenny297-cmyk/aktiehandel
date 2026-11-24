"""Marknadsdata: fel från datakällan, saknade tickers, lookahead och gammal data.

Inget test rör nätverket. En fejkad downloader matar in kursdata i stället.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from aiaktier.market_data import DataUnavailable, check_staleness, fetch_snapshot
from aiaktier.trading_calendar import TradingCalendar

BENCH = "^OMX"
NOW = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)   # fredag


def make_frame(series_by_ticker: dict, end: date = date(2026, 11, 20)) -> pd.DataFrame:
    """Bygg ett yfinance-liknande svar med MultiIndex-kolumner."""
    lengths = [len(v) for v in series_by_ticker.values()]
    n = max(lengths)
    index = pd.bdate_range(end=pd.Timestamp(end), periods=n)

    frame = {}
    for ticker, closes in series_by_ticker.items():
        padded = [None] * (n - len(closes)) + list(closes)
        for column in ("Open", "High", "Low", "Close"):
            frame[(ticker, column)] = padded
        frame[(ticker, "Volume")] = [1000] * n

    df = pd.DataFrame(frame, index=index)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def downloader_for(series_by_ticker: dict, end: date = date(2026, 11, 20)):
    def _download(symbols, period_days):
        return make_frame(series_by_ticker, end)
    return _download


@pytest.fixture
def cal() -> TradingCalendar:
    return TradingCalendar.from_config(
        {
            "schedule": {"timezone": "Europe/Stockholm", "slots": {"close": "17:00"}},
            "market": {"holidays": []},
        }
    )


# --------------------------------------------------------- normalfall


def test_snapshot_plockar_ut_senaste_pris_och_historik():
    data = {
        "ERIC-B.ST": [90.0, 92.0, 94.0],
        "VOLV-B.ST": [250.0, 255.0, 260.0],
        BENCH: [2500.0, 2520.0, 2540.0],
    }
    snap = fetch_snapshot(
        ["ERIC-B.ST", "VOLV-B.ST"], BENCH,
        now=NOW, history_days=60, downloader=downloader_for(data),
    )

    assert snap.prices == {"ERIC-B.ST": 94.0, "VOLV-B.ST": 260.0}
    assert snap.benchmark_price == 2540.0
    # Index ligger inte bland aktierna.
    assert BENCH not in snap.prices
    assert BENCH not in snap.history
    assert len(snap.history["ERIC-B.ST"]) == 3
    assert snap.missing == ()


def test_foregaende_stangning_ar_naest_sista_baren():
    data = {"ERIC-B.ST": [90.0, 92.0, 94.0], BENCH: [2500.0, 2520.0, 2540.0]}
    snap = fetch_snapshot(
        ["ERIC-B.ST"], BENCH, now=NOW, history_days=60,
        downloader=downloader_for(data),
    )

    assert snap.previous_close("ERIC-B.ST") == 92.0
    assert snap.benchmark_previous_close() == 2520.0
    assert snap.previous_close("SAKNAS.ST") is None


def test_historiken_kapas_till_onskat_antal_dagar():
    data = {"ERIC-B.ST": list(range(100, 200)), BENCH: list(range(2000, 2100))}
    snap = fetch_snapshot(
        ["ERIC-B.ST"], BENCH, now=NOW, history_days=10,
        downloader=downloader_for(data),
    )

    assert len(snap.history["ERIC-B.ST"]) == 10
    assert snap.history["ERIC-B.ST"][-1].close == 199.0


# ------------------------------------------------------- ingen lookahead


def test_rader_efter_korningens_datum_kastas_bort():
    # Datan sträcker sig till 20 november, men körningen sker den 18:e.
    data = {"ERIC-B.ST": [90.0, 92.0, 94.0], BENCH: [2500.0, 2520.0, 2540.0]}
    tidigare = datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc)

    snap = fetch_snapshot(
        ["ERIC-B.ST"], BENCH, now=tidigare, history_days=60,
        downloader=downloader_for(data),
    )

    # Bara baren från den 18:e finns kvar, och priset är den dagens.
    assert snap.last_bar_date("ERIC-B.ST") == date(2026, 11, 18)
    assert snap.prices["ERIC-B.ST"] == 90.0
    assert all(bar.day <= date(2026, 11, 18) for bar in snap.history["ERIC-B.ST"])


# ----------------------------------------------- fel från datakällan


def test_nedladdningsfel_blir_dataunavailable():
    def trasig(symbols, period_days):
        raise ConnectionError("namnuppslag misslyckades")

    with pytest.raises(DataUnavailable) as exc:
        fetch_snapshot(["ERIC-B.ST"], BENCH, now=NOW, attempts=1, downloader=trasig)

    assert "namnuppslag" in str(exc.value)


def test_tomt_svar_blir_dataunavailable():
    def tomt(symbols, period_days):
        return pd.DataFrame()

    with pytest.raises(DataUnavailable):
        fetch_snapshot(["ERIC-B.ST"], BENCH, now=NOW, attempts=1, downloader=tomt)


def test_saknat_index_blir_dataunavailable():
    data = {"ERIC-B.ST": [90.0, 92.0], "VOLV-B.ST": [250.0, 255.0]}
    with pytest.raises(DataUnavailable) as exc:
        fetch_snapshot(
            ["ERIC-B.ST", "VOLV-B.ST"], BENCH, now=NOW, attempts=1,
            downloader=downloader_for(data),
        )

    assert BENCH in str(exc.value)


def test_enstaka_saknad_ticker_rapporteras_men_stoppar_inte_rundan():
    data = {
        "ERIC-B.ST": [90.0, 92.0],
        "VOLV-B.ST": [250.0, 255.0],
        "ABB.ST": [None, None],
        BENCH: [2500.0, 2520.0],
    }
    snap = fetch_snapshot(
        ["ERIC-B.ST", "VOLV-B.ST", "ABB.ST"], BENCH, now=NOW,
        downloader=downloader_for(data),
    )

    assert snap.missing == ("ABB.ST",)
    assert "ABB.ST" not in snap.prices
    assert snap.available(["ERIC-B.ST", "ABB.ST"]) == ["ERIC-B.ST"]


def test_for_manga_saknade_tickers_stoppar_rundan():
    data = {
        "ERIC-B.ST": [90.0, 92.0],
        "VOLV-B.ST": [None, None],
        "ABB.ST": [None, None],
        "AZN.ST": [None, None],
        BENCH: [2500.0, 2520.0],
    }
    with pytest.raises(DataUnavailable) as exc:
        fetch_snapshot(
            ["ERIC-B.ST", "VOLV-B.ST", "ABB.ST", "AZN.ST"], BENCH, now=NOW, attempts=1,
            downloader=downloader_for(data),
        )

    assert "1 av 4" in str(exc.value)


def test_nollpriser_raknas_som_saknad_data():
    data = {"ERIC-B.ST": [0.0, 0.0], "VOLV-B.ST": [250.0, 255.0], BENCH: [2500.0, 2520.0]}
    snap = fetch_snapshot(
        ["ERIC-B.ST", "VOLV-B.ST"], BENCH, now=NOW,
        downloader=downloader_for(data),
    )
    assert "ERIC-B.ST" in snap.missing


# ---------------------------------------------------------- gammal data


def test_fardsk_data_ar_inte_gammal(cal):
    data = {"ERIC-B.ST": [90.0, 92.0], BENCH: [2500.0, 2520.0]}
    snap = fetch_snapshot(
        ["ERIC-B.ST"], BENCH, now=NOW, downloader=downloader_for(data),
    )
    assert check_staleness(snap, cal, max_staleness_days=3) == []


def test_gammal_data_flaggas(cal):
    # Senaste baren är från 2 november, körningen sker 20 november.
    data = {"ERIC-B.ST": [90.0, 92.0], BENCH: [2500.0, 2520.0]}
    snap = fetch_snapshot(
        ["ERIC-B.ST"], BENCH, now=NOW,
        downloader=downloader_for(data, end=date(2026, 11, 2)),
    )
    assert check_staleness(snap, cal, max_staleness_days=3) == ["ERIC-B.ST"]


def test_helg_gor_inte_data_gammal(cal):
    # Bar från fredag, körning på måndag: en handelsdag emellan, inte tre.
    data = {"ERIC-B.ST": [90.0, 92.0], BENCH: [2500.0, 2520.0]}
    snap = fetch_snapshot(
        ["ERIC-B.ST"], BENCH,
        now=datetime(2026, 11, 23, 8, 15, tzinfo=timezone.utc),
        downloader=downloader_for(data, end=date(2026, 11, 20)),
    )
    assert check_staleness(snap, cal, max_staleness_days=1) == []


# ------------------------------------------------------- omförsök mot Yahoo


class Sequenced:
    """En downloader som svarar olika på varje anrop, för att efterlikna en
    datakälla som strypsätter först och släpper igenom sedan."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, symbols, period_days):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


GOOD = {"ERIC-B.ST": [90.0, 92.0], "VOLV-B.ST": [250.0, 255.0], BENCH: [2500.0, 2520.0]}
HALF = {"ERIC-B.ST": [None, None], "VOLV-B.ST": [None, None], BENCH: [2500.0, 2520.0]}


def fetch_with(downloader, sleeps, **kwargs):
    options = {"now": NOW, "downloader": downloader, "sleep_fn": sleeps.append}
    options.update(kwargs)
    return fetch_snapshot(["ERIC-B.ST", "VOLV-B.ST"], BENCH, **options)


def test_lyckas_pa_andra_forsoket_efter_natverksfel():
    sleeps = []
    source = Sequenced(ConnectionError("429 Too Many Requests"), make_frame(GOOD))

    snap = fetch_with(source, sleeps)

    assert snap.prices["ERIC-B.ST"] == 92.0
    assert source.calls == 2
    assert sleeps == [5.0]


def test_tomt_svar_gors_om():
    # Strypning ger ofta en tom tabell i stället för ett undantag.
    sleeps = []
    source = Sequenced(pd.DataFrame(), pd.DataFrame(), make_frame(GOOD))

    snap = fetch_with(source, sleeps)

    assert snap.prices["VOLV-B.ST"] == 255.0
    assert source.calls == 3
    assert sleeps == [5.0, 15.0]


def test_halvtomt_svar_gors_om():
    # Index kom med men inga aktier: lika oanvändbart som inget svar alls.
    sleeps = []
    source = Sequenced(make_frame(HALF), make_frame(GOOD))

    snap = fetch_with(source, sleeps)

    assert source.calls == 2
    assert snap.missing == ()
    assert sleeps == [5.0]


def test_vantetiden_vaxer_och_ingen_somn_efter_sista_forsoket():
    sleeps = []
    source = Sequenced(ConnectionError("nere"))

    with pytest.raises(DataUnavailable):
        fetch_with(source, sleeps, attempts=4)

    assert source.calls == 4
    # Tre pauser mellan fyra försök. Ingen paus efter det sista.
    assert sleeps == [5.0, 15.0, 45.0]


def test_backoff_gar_att_konfigurera():
    sleeps = []
    source = Sequenced(ConnectionError("nere"))

    with pytest.raises(DataUnavailable):
        fetch_with(source, sleeps, attempts=3, backoff_seconds=2.0, backoff_factor=2.0)

    assert sleeps == [2.0, 4.0]


def test_ingen_vantan_nar_forsta_forsoket_lyckas():
    sleeps = []
    source = Sequenced(make_frame(GOOD))

    fetch_with(source, sleeps)

    assert source.calls == 1
    assert sleeps == []


def test_felet_fran_sista_forsoket_ar_det_som_kastas():
    sleeps = []
    source = Sequenced(ConnectionError("forsta"), pd.DataFrame())

    with pytest.raises(DataUnavailable) as exc:
        fetch_with(source, sleeps, attempts=2)

    assert "tom" in str(exc.value)
    assert "forsta" not in str(exc.value)


def test_ursprungligt_undantag_bevaras_som_orsak():
    source = Sequenced(ConnectionError("dns"))

    with pytest.raises(DataUnavailable) as exc:
        fetch_with(source, [], attempts=1)

    assert isinstance(exc.value.__cause__, ConnectionError)


def test_ett_enda_forsok_betyder_inga_omforsok():
    sleeps = []
    source = Sequenced(ConnectionError("nere"), make_frame(GOOD))

    with pytest.raises(DataUnavailable):
        fetch_with(source, sleeps, attempts=1)

    assert source.calls == 1
    assert sleeps == []


def test_noll_forsok_ar_en_konfigurationsbugg():
    with pytest.raises(ValueError):
        fetch_with(Sequenced(make_frame(GOOD)), [], attempts=0)
