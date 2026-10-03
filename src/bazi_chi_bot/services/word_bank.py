"""Word sources and durable per-player bot-word history."""

from __future__ import annotations

import secrets
import time

import aiosqlite

from ..rules import normalize_word
from ..word_bank import BOT_WORDS

COMPOUND_PREFIXES = ("ابر", "فسقلی", "جادو", "فضایی", "شیطون", "خفن", "ناز", "مینی")


async def record_player_word(
    connection: aiosqlite.Connection, user_id: int, word: str
) -> None:
    await connection.execute(
        """INSERT OR IGNORE INTO human_word_submissions (word, user_id, created_at)
           VALUES (?, ?, ?)""",
        (normalize_word(word), user_id, int(time.time())),
    )


async def choose_unused_bot_word(
    connection: aiosqlite.Connection, user_id: int, preferred: str | None = None
) -> str:
    """Choose a word unseen by this player, including previous daily challenges."""
    rows = await (
        await connection.execute(
            "SELECT word FROM bot_word_assignments WHERE user_id = ?", (user_id,)
        )
    ).fetchall()
    seen = {row["word"] for row in rows}
    human_rows = await (
        await connection.execute(
            """SELECT DISTINCT word FROM human_word_submissions
               WHERE user_id != ?""", (user_id,)
        )
    ).fetchall()
    human = [row["word"] for row in human_rows if row["word"] not in seen]
    if preferred is not None:
        preferred = normalize_word(preferred)
        if preferred not in seen:
            # A shared daily word remains an option, while player submissions
            # also contribute to daily rounds when available.
            if human and secrets.randbelow(2):
                return secrets.choice(human)
            return preferred
    curated = [word for word in BOT_WORDS if word not in seen]
    if human and curated:
        return secrets.choice(human if secrets.randbelow(2) else curated)
    if human or curated:
        return secrets.choice(human or curated)

    # Keep the no-repeat promise even after the initial bank is exhausted.
    for prefix in COMPOUND_PREFIXES:
        for base in BOT_WORDS:
            compound = f"{prefix}{base}"
            if 2 <= len(compound) <= 20 and compound not in seen:
                return compound
    for left in BOT_WORDS:
        for right in BOT_WORDS:
            compound = f"{left}{right}"
            if 2 <= len(compound) <= 20 and compound not in seen:
                return compound
    raise RuntimeError("The bot word bank has no unused word for this player")
