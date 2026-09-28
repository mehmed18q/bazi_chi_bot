"""Calendar helpers for the monthly player leaderboard."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo


LEADERBOARD_TIMEZONE = ZoneInfo("Asia/Tehran")


def leaderboard_period_start(timestamp: int | float | None = None) -> datetime:
    """Return the start of the current leaderboard month in Tehran time."""
    instant = time.time() if timestamp is None else timestamp
    local = datetime.fromtimestamp(instant, tz=LEADERBOARD_TIMEZONE)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def leaderboard_period_start_epoch(timestamp: int | float | None = None) -> int:
    return int(leaderboard_period_start(timestamp).timestamp())


def next_leaderboard_reset(timestamp: int | float | None = None) -> datetime:
    """Return the next reset instant, preserving the configured local timezone."""
    start = leaderboard_period_start(timestamp)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def next_leaderboard_reset_label(timestamp: int | float | None = None) -> str:
    reset = next_leaderboard_reset(timestamp)
    return reset.strftime("%Y/%m/%d، ساعت %H:%M")
