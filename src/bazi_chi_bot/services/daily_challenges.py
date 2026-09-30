"""Shared daily challenge configuration, participation, and notification ledger."""

from __future__ import annotations

import json
import secrets
import time
from datetime import date

from ..daily_schedule import challenge_window, local_date
from ..db import Database
from ..errors import DailyChallengeClosed
from ..models import DailyChallenge, Game, GameType
from ..persistence.mappers import _game_from_row
from ..rules import ALLOWED_HAND_COUNTS, MASTERMIND_COLORS
from .matches import MatchService
from .solo import BOT_WORDS

DAILY_TYPES = (
    GameType.GOL_YA_POOCH,
    GameType.TIC_TAC_TOE,
    GameType.WORD_GUESS,
    GameType.MASTERMIND,
)


def _challenge_from_row(row) -> DailyChallenge:
    return DailyChallenge(
        challenge_date=row["challenge_date"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        game_type=GameType(row["game_type"]),
        fists=row["fists"],
        total_hands=row["total_hands"],
        bot_starts=bool(row["bot_starts"]),
        secrets=tuple(json.loads(row["secrets_json"])),
    )


class DailyChallengeService:
    def __init__(self, database: Database, matches: MatchService) -> None:
        self.database = database
        self.matches = matches

    @staticmethod
    def window_status(now: int | None = None) -> str:
        current = int(time.time()) if now is None else now
        start, end = challenge_window(local_date(current))
        return "before" if current < start else "open" if current < end else "ended"

    async def get(self, day: date) -> DailyChallenge | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM daily_challenges WHERE challenge_date = ?", (day.isoformat(),)
                )
            ).fetchone()
        return _challenge_from_row(row) if row else None

    async def ensure_today(self, *, now: int | None = None) -> DailyChallenge:
        current = int(time.time()) if now is None else now
        if self.window_status(current) != "open":
            raise DailyChallengeClosed
        day = local_date(current)
        start, end = challenge_window(day)
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM daily_challenges WHERE challenge_date = ?", (day.isoformat(),)
                )
            ).fetchone()
            if row is None:
                game_type = secrets.choice(DAILY_TYPES)
                total_hands = secrets.choice(tuple(sorted(ALLOWED_HAND_COUNTS)))
                fists = (
                    8
                    if game_type is GameType.MASTERMIND
                    else secrets.randbelow(5) + 2
                    if game_type is GameType.GOL_YA_POOCH
                    else 2
                )
                bot_starts = secrets.randbelow(2)
                if game_type is GameType.GOL_YA_POOCH:
                    hidden = [secrets.randbelow(fists) + 1 for _ in range(total_hands)]
                elif game_type is GameType.WORD_GUESS:
                    hidden = [secrets.choice(BOT_WORDS) for _ in range(total_hands)]
                elif game_type is GameType.MASTERMIND:
                    hidden = [
                        [secrets.choice(MASTERMIND_COLORS) for _ in range(4)]
                        for _ in range(total_hands)
                    ]
                else:
                    hidden = []
                await connection.execute(
                    """
                    INSERT INTO daily_challenges (
                        challenge_date, starts_at, ends_at, game_type, fists,
                        total_hands, bot_starts, secrets_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        day.isoformat(),
                        start,
                        end,
                        game_type.value,
                        fists,
                        total_hands,
                        bot_starts,
                        json.dumps(hidden, ensure_ascii=False),
                        current,
                    ),
                )
                row = await (
                    await connection.execute(
                        "SELECT * FROM daily_challenges WHERE challenge_date = ?",
                        (day.isoformat(),),
                    )
                ).fetchone()
        return _challenge_from_row(row)

    async def participation(self, day: date, user_id: int) -> Game | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM games WHERE daily_challenge_date = ? AND creator_id = ?",
                    (day.isoformat(), user_id),
                )
            ).fetchone()
        return _game_from_row(row) if row else None

    async def start(self, user_id: int) -> Game:
        challenge = await self.ensure_today()
        return await self.matches.create_solo_game(
            user_id,
            challenge.fists,
            challenge.total_hands,
            challenge.game_type,
            daily_date=challenge.challenge_date,
            bot_starts=challenge.bot_starts,
        )

    async def close_day(self, day: date, *, now: int | None = None) -> None:
        current = int(time.time()) if now is None else now
        _, end = challenge_window(day)
        if current < end:
            return
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE games SET status = 'cancelled', phase = 'cancelled',
                    version = version + 1, updated_at = ?
                WHERE daily_challenge_date = ? AND status = 'active'
                """,
                (current, day.isoformat()),
            )

    async def queue_notifications(self, challenge: DailyChallenge, event: str) -> None:
        if event not in ("start", "end"):
            raise ValueError("Unknown daily notification event")
        current = int(time.time())
        if event == "start" and not challenge.starts_at <= current < challenge.ends_at:
            return
        if event == "end" and current < challenge.ends_at:
            return
        async with self.database.transaction() as connection:
            if event == "end":
                row = await (
                    await connection.execute(
                        "SELECT end_queued_at FROM daily_challenges WHERE challenge_date = ?",
                        (challenge.challenge_date,),
                    )
                ).fetchone()
                if row is None or row["end_queued_at"] is not None:
                    return
            await connection.execute(
                """
                INSERT OR IGNORE INTO daily_notifications (challenge_date, user_id, event)
                SELECT ?, telegram_id, ? FROM users
                WHERE is_activated = 1 AND telegram_id != -1
                """,
                (challenge.challenge_date, event),
            )
            if event == "end":
                await connection.execute(
                    "UPDATE daily_challenges SET end_queued_at = ? WHERE challenge_date = ?",
                    (current, challenge.challenge_date),
                )

    async def next_notification(self) -> tuple[str, int, str] | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT challenge_date, user_id, event FROM daily_notifications
                    WHERE delivered_at IS NULL
                    ORDER BY challenge_date, CASE event WHEN 'start' THEN 0 ELSE 1 END, user_id
                    LIMIT 1
                    """
                )
            ).fetchone()
        return (row["challenge_date"], row["user_id"], row["event"]) if row else None

    async def record_notification(self, day: str, user_id: int, event: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE daily_notifications SET delivered_at = ?
                WHERE challenge_date = ? AND user_id = ? AND event = ?
                    AND delivered_at IS NULL
                """,
                (int(time.time()), day, user_id, event),
            )

    async def notification_text(self, day: str, user_id: int, event: str) -> str:
        challenge = await self.get(date.fromisoformat(day))
        if challenge is None:
            raise RuntimeError("Daily challenge does not exist")
        titles = {
            GameType.GOL_YA_POOCH: "گل یا پوچ",
            GameType.TIC_TAC_TOE: "دوز سه‌تایی",
            GameType.WORD_GUESS: "حدس کلمه",
            GameType.MASTERMIND: "فکر بکر",
        }
        title = titles[challenge.game_type]
        unit = (
            "دست" if challenge.game_type in (GameType.GOL_YA_POOCH, GameType.TIC_TAC_TOE) else "دور"
        )
        if event == "start":
            game = await self.participation(date.fromisoformat(day), user_id)
            if game is None:
                invitation = "بیا بازی کن تا جا نمونی!"
            elif game.status.value == "finished" and game.winner_id == user_id:
                invitation = "تو چالش را برده‌ای و امتیاز امروزت ثبت شده است. 🏆"
            elif game.status.value == "finished":
                invitation = "تو بازی کرده‌ای؛ تلاش امروزت به پایان رسیده است."
            else:
                invitation = "بازی‌ات شروع شده؛ از «ادامهٔ بازی» آن را کامل کن."
            return (
                f"🎯 <b>چالش روزانه شروع شد!</b>\nبازی امروز: <b>{title}</b>، "
                f"{challenge.total_hands} {unit}. تا ساعت ۱۳ وقت داری. {invitation}"
            )
        game = await self.participation(date.fromisoformat(day), user_id)
        if game is not None and game.status.value == "finished" and game.winner_id == user_id:
            result = "🏆 دست‌خوش! چالش امروز را بردی و یک امتیاز گرفتی."
        elif game is not None:
            result = "🌤 در چالش شرکت کردی، اما امتیاز امروز را نگرفتی. فردا دوباره تلاش کن."
        else:
            result = "⌛ در چالش امروز شرکت نکردی و امتیازش را از دست دادی. فردا دوباره بیا!"
        detail = ""
        if challenge.secrets:
            first_bot_hand = 0 if challenge.bot_starts else 1
            bot_hands = [
                (index + 1, challenge.secrets[index])
                for index in range(first_bot_hand, len(challenge.secrets), 2)
            ]
            if challenge.game_type is GameType.WORD_GUESS:
                detail = "\n🔐 کلمه‌های مخفی ربات: " + "، ".join(
                    f"دور {number}: <b>{secret}</b>" for number, secret in bot_hands
                )
            elif challenge.game_type is GameType.MASTERMIND:
                from ..rules import MASTERMIND_COLOR_EMOJIS

                detail = "\n🎨 کدهای مخفی ربات: " + "، ".join(
                    f"دور {number}: " + " ".join(MASTERMIND_COLOR_EMOJIS[color] for color in secret)
                    for number, secret in bot_hands
                )
            elif challenge.game_type is GameType.GOL_YA_POOCH:
                detail = "\n🌸 مشت‌های دارای گل ربات: " + "، ".join(
                    f"دست {number}: <b>{secret}</b>" for number, secret in bot_hands
                )
        return f"🏁 <b>چالش امروز تمام شد.</b>\n{result}\nبازی امروز: <b>{title}</b>.{detail}"
