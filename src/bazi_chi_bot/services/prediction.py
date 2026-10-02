"""Learn human choices from completed, revealed rounds only."""

from __future__ import annotations

import secrets
import time

import aiosqlite

from ..models import GameType

RPS_CHOICES = ("rock", "paper", "scissors")
RPS_COUNTER = {"rock": "paper", "paper": "scissors", "scissors": "rock"}


def hand_bucket(hand_number: int, total_hands: int) -> int:
    """Group opening, middle, and closing hands for similar-position patterns."""
    if hand_number == 1:
        return 0
    if hand_number == total_hands:
        return 2
    return 1


async def record_choice(
    connection: aiosqlite.Connection,
    *,
    game_id: int,
    hand_number: int,
    total_hands: int,
    user_id: int,
    game_type: GameType,
    choice: str,
    option_count: int,
) -> None:
    if user_id <= 0:
        return
    previous = await (
        await connection.execute(
            """SELECT choice FROM choice_observations
               WHERE user_id = ? AND game_type = ? AND option_count = ?
               ORDER BY id DESC LIMIT 1""",
            (user_id, game_type.value, option_count),
        )
    ).fetchone()
    await connection.execute(
        """INSERT INTO choice_observations
           (game_id, hand_number, user_id, game_type, choice, previous_choice,
            option_count, hand_bucket, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            game_id,
            hand_number,
            user_id,
            game_type.value,
            choice,
            previous["choice"] if previous else None,
            option_count,
            hand_bucket(hand_number, total_hands),
            int(time.time()),
        ),
    )


async def choice_probabilities(
    connection: aiosqlite.Connection,
    *,
    user_id: int,
    game_type: GameType,
    options: tuple[str, ...],
    hand_number: int,
    total_hands: int,
) -> dict[str, float]:
    """Blend personal and population frequencies with transition and hand context.

    Smoothing keeps a few observations from dominating. Selection adds a small
    exploration rate so the bot is not fully predictable.
    """
    option_count = len(options)
    global_rows = await (
        await connection.execute(
            """SELECT id, user_id, choice, previous_choice, hand_bucket
               FROM choice_observations
               WHERE game_type = ? AND option_count = ?
               ORDER BY id DESC LIMIT 3000""",
            (game_type.value, option_count),
        )
    ).fetchall()
    personal_rows = await (
        await connection.execute(
            """SELECT id, user_id, choice, previous_choice, hand_bucket
               FROM choice_observations
               WHERE user_id = ? AND game_type = ? AND option_count = ?
               ORDER BY id DESC LIMIT 200""",
            (user_id, game_type.value, option_count),
        )
    ).fetchall()
    rows = sorted(
        {row["id"]: row for row in (*global_rows, *personal_rows)}.values(),
        key=lambda row: row["id"],
        reverse=True,
    )
    previous = next((row["choice"] for row in rows if row["user_id"] == user_id), None)
    bucket = hand_bucket(hand_number, total_hands)
    base = 1 / option_count
    scores = {choice: base for choice in options}
    groups = (
        (
            lambda row: (
                row["user_id"] == user_id
                and row["previous_choice"] == previous
                and row["hand_bucket"] == bucket
                and previous is not None
            ),
            8,
            3,
        ),
        (
            lambda row: (
                row["user_id"] == user_id
                and row["previous_choice"] == previous
                and previous is not None
            ),
            5,
            4,
        ),
        (lambda row: row["user_id"] == user_id and row["hand_bucket"] == bucket, 4, 5),
        (lambda row: row["user_id"] == user_id, 3, 6),
        (
            lambda row: (
                row["previous_choice"] == previous
                and row["hand_bucket"] == bucket
                and previous is not None
            ),
            2,
            15,
        ),
        (lambda row: row["hand_bucket"] == bucket, 1, 20),
        (lambda row: True, 1, 30),
    )
    for matches, influence, smoothing in groups:
        counts = {choice: 0 for choice in options}
        for row in rows:
            if row["choice"] in counts and matches(row):
                counts[row["choice"]] += 1
        total = sum(counts.values())
        if total:
            confidence = influence * total / (total + smoothing)
            for choice in options:
                scores[choice] += confidence * counts[choice] / total
    denominator = sum(scores.values())
    return {choice: value / denominator for choice, value in scores.items()}


async def predict_choice(
    connection: aiosqlite.Connection,
    *,
    user_id: int,
    game_type: GameType,
    options: tuple[str, ...],
    hand_number: int,
    total_hands: int,
) -> str:
    scores = await choice_probabilities(
        connection,
        user_id=user_id,
        game_type=game_type,
        options=options,
        hand_number=hand_number,
        total_hands=total_hands,
    )
    if secrets.randbelow(10) == 0:
        return options[secrets.randbelow(len(options))]
    highest = max(scores.values())
    best = tuple(choice for choice in options if scores[choice] == highest)
    return best[secrets.randbelow(len(best))]


async def choose_rps_response(
    connection: aiosqlite.Connection, user_id: int, hand_number: int, total_hands: int
) -> str:
    scores = await choice_probabilities(
        connection,
        user_id=user_id,
        game_type=GameType.ROCK_PAPER_SCISSORS,
        options=RPS_CHOICES,
        hand_number=hand_number,
        total_hands=total_hands,
    )
    if secrets.randbelow(10) == 0:
        return RPS_CHOICES[secrets.randbelow(3)]
    values = {
        response: scores[beaten] - scores[RPS_COUNTER[response]]
        for beaten, response in RPS_COUNTER.items()
    }
    highest = max(values.values())
    best = tuple(choice for choice in RPS_CHOICES if values[choice] == highest)
    return best[secrets.randbelow(len(best))]
