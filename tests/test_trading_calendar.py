"""Tidsfönster: sommartid, vintertid, helg, helgdag, halvdag och dedupe."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aiaktier.trading_calendar import (
    SKIP_AFTER_CLOSE,
    SKIP_HOLIDAY,
    SKIP_NO_SLOT,
    SKIP_WEEKEND,
    TradingCalendar,
    already_ran,
    record_run,
)

CONFIG = {
    "schedule": {
        "timezone": "Europe/Stockholm",
        "slots": {"morning": "09:15", "midday": "12:30", "close": "17:00"},
        "tolerance_minutes": 75,
    },
    "market": {
        "open": "09:00",
        "close": "17:30",
        "holidays": ["2026-12-24", "2026-12-25", "2027-01-01"],
        "early_close": {"2026-12-23": "13:00"},
    },
}


@pytest.fixture
def cal() -> TradingCalendar:
    return TradingCalendar.from_config(CONFIG)


def utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ------------------------------------------------- sommartid / vintertid


def test_sommartid_0715_utc_ar_morgonsloten(cal):
    # Juni: Sverige ligger UTC+2, så 07:15 UTC är 09:15 lokalt.
    decision = cal.match_slot(utc(2026, 6, 10, 7, 15))
    assert decision.ok
    assert decision.slot_name == "morning"


def test_vintertid_0815_utc_ar_morgonsloten(cal):
    # November: UTC+1, så 08:15 UTC är 09:15 lokalt.
    decision = cal.match_slot(utc(2026, 11, 10, 8, 15))
    assert decision.ok
    assert decision.slot_name == "morning"


def test_sommartidens_cronpost_avvisas_pa_vintern(cal):
    # 07:15 UTC i november är 08:15 lokalt, en timme före morgonsloten.
    decision = cal.match_slot(utc(2026, 11, 10, 7, 15))
    assert not decision.ok
    assert decision.reason == SKIP_NO_SLOT


def test_vintertidens_cronpost_avvisas_pa_sommaren(cal):
    # 15:00 UTC i juni är 17:00 lokalt och matchar stängningssloten, medan
    # 16:00 UTC är 18:00 lokalt, en timme för sent för 17:00-slotens fönster.
    assert cal.match_slot(utc(2026, 6, 10, 15, 0)).slot_name == "close"
    sent = cal.match_slot(utc(2026, 6, 10, 16, 20))
    assert not sent.ok
    assert sent.reason == SKIP_NO_SLOT


def test_alla_tre_slottar_matchar_pa_sina_klockslag(cal):
    for utc_hour, name in ((8, "morning"), (11, "midday"), (16, "close")):
        minute = {8: 15, 11: 30, 16: 0}[utc_hour]
        decision = cal.match_slot(utc(2026, 11, 10, utc_hour, minute))
        assert decision.ok
        assert decision.slot_name == name


# ----------------------------------------------------- fördröjd cron


def test_forsenad_korning_inom_toleransen_accepteras(cal):
    # 40 minuter sent: fortfarande morgonsloten.
    decision = cal.match_slot(utc(2026, 11, 10, 8, 55))
    assert decision.ok
    assert decision.slot_name == "morning"


def test_korning_strax_innan_sloten_avvisas(cal):
    decision = cal.match_slot(utc(2026, 11, 10, 8, 14))
    assert not decision.ok
    assert decision.reason == SKIP_NO_SLOT


def test_korning_efter_toleransfonstret_avvisas(cal):
    # 09:15 + 75 minuter = 10:30 lokalt. 10:31 är utanför.
    decision = cal.match_slot(utc(2026, 11, 10, 9, 31))
    assert not decision.ok
    assert decision.reason == SKIP_NO_SLOT


# --------------------------------------------------- helg och helgdag


def test_helg_hoppas_over(cal):
    lordag = cal.match_slot(utc(2026, 11, 14, 8, 15))
    sondag = cal.match_slot(utc(2026, 11, 15, 8, 15))
    assert not lordag.ok and lordag.reason == SKIP_WEEKEND
    assert not sondag.ok and sondag.reason == SKIP_WEEKEND


def test_helgdag_hoppas_over(cal):
    decision = cal.match_slot(utc(2026, 12, 24, 8, 15))
    assert not decision.ok
    assert decision.reason == SKIP_HOLIDAY


def test_is_trading_day_kanner_bade_helg_och_helgdag(cal):
    assert cal.is_trading_day(date(2026, 11, 10))
    assert not cal.is_trading_day(date(2026, 11, 14))     # lördag
    assert not cal.is_trading_day(date(2026, 12, 25))     # helgdag


# ------------------------------------------------------------ halvdag


def test_slot_efter_tidig_stangning_hoppas_over(cal):
    # 23 december stänger börsen 13:00, så 17:00-sloten är ogiltig.
    decision = cal.match_slot(utc(2026, 12, 23, 16, 0))
    assert not decision.ok
    assert decision.reason == SKIP_AFTER_CLOSE
    assert decision.slot_name == "close"


def test_slottar_fore_tidig_stangning_galler_anda(cal):
    assert cal.match_slot(utc(2026, 12, 23, 8, 15)).ok
    assert cal.match_slot(utc(2026, 12, 23, 11, 30)).ok


def test_close_time_ar_ordinarie_pa_vanliga_dagar(cal):
    assert cal.close_time(date(2026, 11, 10)).hour == 17
    assert cal.close_time(date(2026, 11, 10)).minute == 30
    assert cal.close_time(date(2026, 12, 23)).hour == 13


# ------------------------------------------------------ handelsdagar


def test_foregaende_handelsdag_hoppar_over_helg(cal):
    # Måndag 16 november: föregående handelsdag är fredag 13 november.
    assert cal.previous_trading_day(date(2026, 11, 16)) == date(2026, 11, 13)


def test_foregaende_handelsdag_hoppar_over_helgdag(cal):
    # Måndag 28 december: 27-26 är helg, 25 och 24 är helgdagar. Kvar blir
    # onsdag 23 december, som är halvdag men fortfarande en handelsdag.
    assert cal.previous_trading_day(date(2026, 12, 28)) == date(2026, 12, 23)


def test_handelsdagar_mellan_raknar_inte_helger(cal):
    # Fredag till måndag är en handelsdag, inte tre.
    assert cal.trading_days_between(date(2026, 11, 13), date(2026, 11, 16)) == 1
    assert cal.trading_days_between(date(2026, 11, 10), date(2026, 11, 10)) == 0
    assert cal.trading_days_between(date(2026, 11, 10), date(2026, 11, 12)) == 2


# ------------------------------------------------------------- dedupe


def test_redan_kord_slot_kanns_igen():
    runs = {"2026-11-23": ["morning"]}
    assert already_ran(runs, date(2026, 11, 23), "morning")
    assert not already_ran(runs, date(2026, 11, 23), "midday")
    assert not already_ran(runs, date(2026, 11, 24), "morning")


def test_record_run_lamnar_originalet_orort():
    runs = {"2026-11-23": ["morning"]}
    updated = record_run(runs, date(2026, 11, 23), "midday")

    assert runs == {"2026-11-23": ["morning"]}
    assert updated["2026-11-23"] == ["morning", "midday"]


def test_record_run_ar_idempotent():
    runs = record_run({}, date(2026, 11, 23), "morning")
    again = record_run(runs, date(2026, 11, 23), "morning")
    assert again["2026-11-23"] == ["morning"]


# ------------------------------------------------------- konfiguration


def test_slottar_sorteras_i_tidsordning(cal):
    assert [s.name for s in cal.slots] == ["morning", "midday", "close"]


def test_tom_slotlista_avvisas():
    with pytest.raises(ValueError):
        TradingCalendar.from_config({"schedule": {"slots": {}}})
