"""Score writes share the caller's transaction with the corresponding game transition."""

import time
from typing import Literal

import aiosqlite


async def award_point(
    connection: aiosqlite.Connection,
    game_id: int,
    user_id: int,
    reason: Literal["round", "challenge"],
    hand_number: int | None = None,
) -> None:
    # Unique event keys provide a second defense against double awards.
    # The database trigger updates the cached user_stats total atomically.
    await connection.execute(
        "INSERT INTO score_events (user_id, game_id, reason, hand_number, amount, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (user_id, game_id, reason, hand_number, int(time.time())),
    )


async def award_login_bonus(
    connection: aiosqlite.Connection, user_id: int, period_start: int
) -> None:
    """Award one idempotent login point for the current leaderboard month."""
    await connection.execute(
        "INSERT OR IGNORE INTO score_events "
        "(user_id, reason, period_start, amount, created_at) VALUES (?, 'login', ?, 1, ?)",
        (user_id, period_start, int(time.time())),
    )


async def record_match_result(
    connection: aiosqlite.Connection, winner_id: int, loser_id: int
) -> None:
    await connection.execute(
        """
        UPDATE user_stats SET games_played = games_played + 1,
            wins = wins + CASE WHEN telegram_id = ? THEN 1 ELSE 0 END,
            losses = losses + CASE WHEN telegram_id = ? THEN 1 ELSE 0 END
        WHERE telegram_id IN (?, ?)
        """,
        (winner_id, loser_id, winner_id, loser_id),
    )


async def record_draw(connection: aiosqlite.Connection, player1_id: int, player2_id: int) -> None:
    """Count a completed match without assigning a win or loss."""
    await connection.execute(
        "UPDATE user_stats SET games_played = games_played + 1 WHERE telegram_id IN (?, ?)",
        (player1_id, player2_id),
    )
