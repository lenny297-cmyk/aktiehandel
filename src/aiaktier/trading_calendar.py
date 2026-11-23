"""Tidsfönster, handelsdagar och slot-matchning.

GitHub Actions cron går i UTC och känner inte till sommartid. Workflowen har
därför poster för båda halvåren, och den här modulen avgör i svensk lokaltid
om en körning faktiskt hör till ett giltigt tidsfönster. Fel halva av året,
helg, helgdag eller en slot som ligger efter dagens stängning: körningen
avvisas här, inte längre fram.

Ren logik: ingen klocka hämtas inuti modulen, `now` skickas alltid in.
Filen heter inte calendar.py för att inte skugga standardbibliotekets modul.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    raise RuntimeError("Python 3.9 eller senare kravs for zoneinfo")


# Orsaker till att en körning avvisas. Loggas ordagrant.
SKIP_WEEKEND = "helg"
SKIP_HOLIDAY = "helgdag"
SKIP_NO_SLOT = "utanfor_tidsfonster"
SKIP_AFTER_CLOSE = "efter_stangning"
SKIP_ALREADY_RUN = "redan_kord"


def parse_hhmm(value: str) -> time:
    hours, minutes = str(value).split(":")
    return time(int(hours), int(minutes))


@dataclass(frozen=True)
class Slot:
    name: str
    at: time

    def __str__(self) -> str:
        return "{} ({:02d}:{:02d})".format(self.name, self.at.hour, self.at.minute)


@dataclass(frozen=True)
class SlotDecision:
    """Utfallet av att matcha en körning mot schemat."""

    ok: bool
    slot: Optional[Slot]
    reason: str
    local_time: datetime

    @property
    def slot_name(self) -> Optional[str]:
        return self.slot.name if self.slot else None


@dataclass(frozen=True)
class TradingCalendar:
    timezone: str
    slots: Tuple[Slot, ...]
    tolerance_minutes: int
    market_open: time
    market_close: time
    holidays: frozenset
    early_close: dict

    # ---------- konstruktion ----------

    @classmethod
    def from_config(cls, cfg: dict) -> "TradingCalendar":
        schedule = cfg.get("schedule", {})
        market = cfg.get("market", {})

        raw_slots = schedule.get("slots", {})
        slots = tuple(
            sorted(
                (Slot(name, parse_hhmm(value)) for name, value in raw_slots.items()),
                key=lambda s: s.at,
            )
        )
        if not slots:
            raise ValueError("minst en slot maste vara konfigurerad")

        return cls(
            timezone=schedule.get("timezone", "Europe/Stockholm"),
            slots=slots,
            tolerance_minutes=int(schedule.get("tolerance_minutes", 60)),
            market_open=parse_hhmm(market.get("open", "09:00")),
            market_close=parse_hhmm(market.get("close", "17:30")),
            holidays=frozenset(
                date.fromisoformat(d) for d in market.get("holidays", [])
            ),
            early_close={
                date.fromisoformat(d): parse_hhmm(t)
                for d, t in (market.get("early_close", {}) or {}).items()
            },
        )

    # ---------- handelsdagar ----------

    @property
    def tz(self):
        return ZoneInfo(self.timezone)

    def localize(self, moment: datetime) -> datetime:
        """Tolka en tidpunkt i börsens tidszon. Naiv tid antas redan vara lokal."""
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.tz)
        return moment.astimezone(self.tz)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in self.holidays

    def is_trading_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def close_time(self, day: date) -> time:
        """Dagens stängningstid. Halvdagar stänger tidigare."""
        return self.early_close.get(day, self.market_close)

    def previous_trading_day(self, day: date) -> date:
        cursor = day - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise ValueError("hittade ingen handelsdag inom 30 dagar fore {}".format(day))

    def trading_days_between(self, start: date, end: date) -> int:
        """Antal handelsdagar efter `start` till och med `end`."""
        if end <= start:
            return 0
        count = 0
        cursor = start + timedelta(days=1)
        while cursor <= end:
            if self.is_trading_day(cursor):
                count += 1
            cursor += timedelta(days=1)
        return count

    # ---------- slot-matchning ----------

    def match_slot(self, now: datetime) -> SlotDecision:
        """Avgör vilken slot en körning hör till, om någon.

        En körning accepteras om den startar på eller efter slottens klockslag
        men inom toleransfönstret. Toleransen finns för att GitHub Actions kan
        försena schemalagda jobb kraftigt.
        """
        local = self.localize(now)
        day = local.date()

        if self.is_weekend(day):
            return SlotDecision(False, None, SKIP_WEEKEND, local)
        if self.is_holiday(day):
            return SlotDecision(False, None, SKIP_HOLIDAY, local)

        closing = self.close_time(day)
        window = timedelta(minutes=self.tolerance_minutes)

        for slot in self.slots:
            slot_start = datetime.combine(day, slot.at, tzinfo=local.tzinfo)
            if slot_start <= local <= slot_start + window:
                if slot.at > closing:
                    # Halvdag: slottar efter stängning hoppas över.
                    return SlotDecision(False, slot, SKIP_AFTER_CLOSE, local)
                return SlotDecision(True, slot, "ok", local)

        return SlotDecision(False, None, SKIP_NO_SLOT, local)


def already_ran(runs: dict, day: date, slot_name: str) -> bool:
    """Har den här sloten redan körts den här dagen?

    `runs` är innehållet i data/runs.json: {"2026-11-23": ["morning", ...]}.
    """
    return slot_name in set(runs.get(day.isoformat(), []))


def record_run(runs: dict, day: date, slot_name: str) -> dict:
    """Returnerar en ny dict med sloten noterad som körd."""
    updated = {key: list(value) for key, value in runs.items()}
    key = day.isoformat()
    entries = updated.setdefault(key, [])
    if slot_name not in entries:
        entries.append(slot_name)
    return updated
