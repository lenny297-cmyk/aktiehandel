"""Jämförelsen mot OMXS30 och de dagliga värdena."""

from __future__ import annotations

import pytest

from aiaktier.benchmark import (
    BenchmarkError,
    DailyValue,
    backfill_official_close,
    index_value,
    performance,
    pct_change,
    upsert_day,
)

START = 100_000.0


def day(date_str, value, index_lvl, index_val, slot="close",
        official=False, cash=0.0, holdings=None):
    return DailyValue(
        day=date_str,
        portfolio_value=value,
        cash=cash,
        market_value=value - cash,
        index_level=index_lvl,
        index_value=index_val,
        slot=slot,
        official_close=official,
        holdings=holdings or {},
    )


# ------------------------------------------------- normalisering av index


def test_index_normaliseras_till_startkapitalet():
    # Oförändrat index betyder oförändrat värde.
    assert index_value(2_500.0, 2_500.0, START) == pytest.approx(100_000.0)
    # Index upp 10 % betyder 110 000 kr.
    assert index_value(2_500.0, 2_750.0, START) == pytest.approx(110_000.0)
    # Index ner 20 % betyder 80 000 kr.
    assert index_value(2_500.0, 2_000.0, START) == pytest.approx(80_000.0)


def test_ogiltiga_indexkurser_avvisas():
    with pytest.raises(BenchmarkError):
        index_value(0.0, 2_500.0, START)
    with pytest.raises(BenchmarkError):
        index_value(2_500.0, 0.0, START)
    with pytest.raises(BenchmarkError):
        index_value(-10.0, 2_500.0, START)


def test_procentuell_forandring():
    assert pct_change(110.0, 100.0) == pytest.approx(10.0)
    assert pct_change(90.0, 100.0) == pytest.approx(-10.0)
    assert pct_change(110.0, 0) is None
    assert pct_change(110.0, None) is None


# ------------------------------------------------------- dagliga värden


def test_upsert_lagger_till_och_sorterar():
    entries = upsert_day([], day("2026-11-20", 100_000, 2_500, 100_000))
    entries = upsert_day(entries, day("2026-11-18", 99_000, 2_480, 99_200))

    assert [e["date"] for e in entries] == ["2026-11-18", "2026-11-20"]


def test_senare_slot_samma_dag_skriver_over():
    entries = upsert_day([], day("2026-11-20", 100_000, 2_500, 100_000, slot="morning"))
    entries = upsert_day(entries, day("2026-11-20", 101_000, 2_510, 100_400, slot="close"))

    assert len(entries) == 1
    assert entries[0]["portfolio_value"] == pytest.approx(101_000)
    assert entries[0]["slot"] == "close"


def test_officiell_stangning_skrivs_inte_over_av_intradag():
    entries = upsert_day([], day("2026-11-20", 101_000, 2_510, 100_400, official=True))
    entries = upsert_day(entries, day("2026-11-20", 99_000, 2_490, 99_600, slot="morning"))

    assert entries[0]["portfolio_value"] == pytest.approx(101_000)
    assert entries[0]["official_close"] is True


def test_innehaven_sparas_med_dagen():
    entries = upsert_day(
        [], day("2026-11-20", 100_000, 2_500, 100_000, holdings={"ERIC-B.ST": 100})
    )
    assert entries[0]["holdings"] == {"ERIC-B.ST": 100}


# --------------------------------------------------------- backfill


def test_backfill_raknar_om_dagen_mot_officiella_kurser():
    # 17:00-mätningen hade ERIC på 100 kr. Officiell stängning blev 104 kr.
    entries = upsert_day(
        [],
        day("2026-11-20", 100_000, 2_500, 100_000, slot="close",
            cash=50_000, holdings={"ERIC-B.ST": 500}),
    )

    updated = backfill_official_close(
        entries, "2026-11-20", {"ERIC-B.ST": 104.0}, 2_550.0, START, 2_500.0
    )

    assert updated[0]["official_close"] is True
    assert updated[0]["slot"] == "backfill"
    assert updated[0]["market_value"] == pytest.approx(52_000.0)
    assert updated[0]["portfolio_value"] == pytest.approx(102_000.0)
    assert updated[0]["index_value"] == pytest.approx(102_000.0)
    # Kassan och innehaven är den dagens, inte dagens som körs nu.
    assert updated[0]["cash"] == pytest.approx(50_000.0)
    assert updated[0]["holdings"] == {"ERIC-B.ST": 500}


def test_backfill_lamnar_dagen_i_fred_om_en_kurs_saknas():
    entries = upsert_day(
        [],
        day("2026-11-20", 100_000, 2_500, 100_000,
            cash=50_000, holdings={"ERIC-B.ST": 500, "VOLV-B.ST": 100}),
    )

    updated = backfill_official_close(
        entries, "2026-11-20", {"ERIC-B.ST": 104.0}, 2_550.0, START, 2_500.0
    )

    assert updated == entries
    assert updated[0]["official_close"] is False


def test_backfill_ror_inte_en_redan_rattad_dag():
    entries = upsert_day(
        [],
        day("2026-11-20", 102_000, 2_550, 102_000, official=True,
            cash=50_000, holdings={"ERIC-B.ST": 500}),
    )

    updated = backfill_official_close(
        entries, "2026-11-20", {"ERIC-B.ST": 90.0}, 2_400.0, START, 2_500.0
    )

    assert updated[0]["portfolio_value"] == pytest.approx(102_000)


def test_backfill_av_okand_dag_gor_ingenting():
    entries = upsert_day([], day("2026-11-20", 100_000, 2_500, 100_000))
    updated = backfill_official_close(entries, "2026-11-19", {}, 2_480.0, START, 2_500.0)
    assert updated == entries


def test_kontantportfolj_backfillas_utan_kurser():
    entries = upsert_day(
        [], day("2026-11-20", 100_000, 2_500, 100_000, cash=100_000, holdings={})
    )
    updated = backfill_official_close(entries, "2026-11-20", {}, 2_550.0, START, 2_500.0)

    assert updated[0]["official_close"] is True
    assert updated[0]["portfolio_value"] == pytest.approx(100_000.0)
    assert updated[0]["index_value"] == pytest.approx(102_000.0)


# ------------------------------------------------------- sammanfattning


def test_prestanda_utan_historik_ar_nollstalld():
    perf = performance([], START)
    assert perf["total_return_pct"] == 0.0
    assert perf["today_pct"] is None


def test_prestanda_sedan_start_och_for_idag():
    entries = upsert_day([], day("2026-11-19", 104_000, 2_550, 102_000))
    entries = upsert_day(entries, day("2026-11-20", 106_080, 2_575, 103_000))

    perf = performance(entries, START)

    assert perf["total_return_pct"] == pytest.approx(6.08)
    assert perf["index_return_pct"] == pytest.approx(3.0)
    assert perf["excess_pct"] == pytest.approx(3.08)
    assert perf["today_pct"] == pytest.approx(2.0)
    assert perf["index_today_pct"] == pytest.approx(0.98)
    assert perf["today_excess_pct"] == pytest.approx(1.02)


def test_portfolj_som_slapar_efter_index_far_negativt_overskott():
    entries = upsert_day([], day("2026-11-20", 98_000, 2_600, 104_000))
    perf = performance(entries, START)

    assert perf["total_return_pct"] == pytest.approx(-2.0)
    assert perf["index_return_pct"] == pytest.approx(4.0)
    assert perf["excess_pct"] == pytest.approx(-6.0)
