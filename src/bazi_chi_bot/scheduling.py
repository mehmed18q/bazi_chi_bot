"""Countdown timing, formatting, and input parsing without network or storage."""

import math
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FINAL_COUNTDOWN_TEXT = (
    "💌✨ <b>زمان انتظار تموم شد!</b> ✨💌\n\n"
    "⏰ دیگه انتظار به پایان رسید...\n\n"
    "❤️ <b>صادق منتظرته</b> ❤️\n\n"
    "بیا که دیگه دلتنگی بسه 🥹🌹\n"
    "وقتشه برسی... 🫶🏻✨"
)


def parse_admin_ids(value: str) -> frozenset[int]:
    """Parse a comma-separated ADMIN_TELEGRAM_IDS setting."""
    if not value.strip():
        return frozenset()
    try:
        return frozenset(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as error:
        raise ValueError("ADMIN_TELEGRAM_IDS must contain comma-separated integers") from error


def parse_target_datetime(value: str, timezone_name: str) -> datetime:
    """Parse an admin-entered local Gregorian date and time."""
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown timezone: {timezone_name}") from error
    try:
        naive = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError as error:
        raise ValueError("Date must use YYYY-MM-DD HH:MM") from error
    return naive.replace(tzinfo=timezone)


def format_remaining(seconds: int | float) -> str:
    """Render MM:SS below one hour and H:MM:SS for longer durations."""
    remaining = max(0, math.ceil(seconds))
    hours, remainder = divmod(remaining, 60 * 60)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"
    return f"{minutes:02d}:{seconds_part:02d}"


def humanize_remaining(seconds: int | float) -> str:
    remaining = max(0, math.ceil(seconds))
    days, remainder = divmod(remaining, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds_part = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes:
        parts.append(f"{minutes} دقیقه")
    if seconds_part or not parts:
        parts.append(f"{seconds_part} ثانیه")
    return " و ".join(parts)


def _persian_digits(value: str) -> str:
    return value.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def meeting_time_text(target_at: int, timezone_name: str) -> str:
    target = datetime.fromtimestamp(target_at, tz=ZoneInfo(timezone_name))
    weekdays = (
        "دوشنبه",
        "سه‌شنبه",
        "چهارشنبه",
        "پنجشنبه",
        "جمعه",
        "شنبه",
        "یکشنبه",
    )
    hour_12 = target.hour % 12 or 12
    period = "صبح" if target.hour < 12 else "بعدازظهر" if target.hour < 18 else "شب"
    clock = _persian_digits(f"{hour_12}:{target.minute:02d}")
    return f"{weekdays[target.weekday()]} ساعت {clock} {period}، وعدهٔ دیدار ماست 💞"


def countdown_message(
    seconds: int | float,
    *,
    target_at: int | None = None,
    timezone_name: str = "Asia/Tehran",
) -> str:
    meeting_line = (
        f"\n\n📅 <b>{meeting_time_text(target_at, timezone_name)}</b>"
        if target_at is not None
        else ""
    )
    return (
        f"⏳ <b>{format_remaining(seconds)}</b> 🥲\n\n"
        f"هنوز <b>{humanize_remaining(seconds)}</b> مونده...\n"
        "ولی هر ثانیه‌ای که می‌گذره، به دیدنت نزدیک‌تر می‌شم ✨🤍"
        f"{meeting_line}"
    )


def next_delivery_at(now: int, target_at: int) -> int:
    """Return the next delivery time, including each faster-cadence boundary."""
    remaining = target_at - now
    if remaining <= 0:
        return now
    if remaining > 60 * 60:
        delay = min(60 * 60, remaining - 60 * 60)
    elif remaining > 10 * 60:
        delay = min(10 * 60, remaining - 10 * 60)
    elif remaining > 60:
        delay = min(5 * 60, remaining - 60)
    else:
        delay = min(10, remaining)
    return now + max(1, delay)
