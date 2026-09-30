"""Tehran-local windows for the shared daily challenge and its reminder."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CHALLENGE_ZONE = ZoneInfo("Asia/Tehran")


def local_date(timestamp: int) -> date:
    return datetime.fromtimestamp(timestamp, CHALLENGE_ZONE).date()


def challenge_window(day: date) -> tuple[int, int]:
    start = datetime.combine(day, time(12), CHALLENGE_ZONE)
    end = datetime.combine(day, time(13), CHALLENGE_ZONE)
    return int(start.timestamp()), int(end.timestamp())


def next_reminder_at(now: int) -> int:
    day = local_date(now)
    target = int(datetime.combine(day, time(11, 55), CHALLENGE_ZONE).timestamp())
    if target <= now:
        target = int(
            datetime.combine(day + timedelta(days=1), time(11, 55), CHALLENGE_ZONE).timestamp()
        )
    return target


def daily_game_open(challenge_date: str, now: int) -> bool:
    start, end = challenge_window(date.fromisoformat(challenge_date))
    return start <= now < end
