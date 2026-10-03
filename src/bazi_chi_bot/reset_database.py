"""Reset game history while preserving Telegram users and the question bank."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Database


async def reset_history(database: Database) -> dict[str, int]:
    await database.initialize()
    async with database.transaction() as connection:
        counts: dict[str, int] = {}
        history_tables = (
            "daily_notifications", "question_answers", "challenge_rounds",
            "choice_observations", "group_attempts", "group_round_moves",
            "group_players", "group_sessions",
            "score_events", "games", "tournaments", "countdowns", "daily_challenges",
        )
        for table in history_tables:
            row = await (await connection.execute(f"SELECT count(*) FROM {table}")).fetchone()
            counts[table] = row[0]

        # Score events are intentionally immutable during normal operation. A full
        # administrative reset is the one explicit exception; triggers are restored
        # by the schema below before the transaction commits.
        await connection.execute("DROP TRIGGER IF EXISTS score_event_no_delete")
        await connection.execute("DROP TRIGGER IF EXISTS answer_matches_game")
        for table in history_tables:
            await connection.execute(f"DELETE FROM {table}")
        await connection.execute(
            """UPDATE user_stats SET games_played = 0, wins = 0, losses = 0,
               correct_guesses = 0, wrong_guesses = 0, points_won = 0"""
        )
        await connection.execute("UPDATE users SET pending_invite_token = NULL")
        await connection.execute(
            """CREATE TRIGGER score_event_no_delete BEFORE DELETE ON score_events
               BEGIN SELECT RAISE(ABORT, 'Score events are immutable'); END"""
        )
        await connection.execute(
            """CREATE TRIGGER answer_matches_game BEFORE INSERT ON question_answers
               BEGIN
                 SELECT CASE WHEN NOT EXISTS (
                   SELECT 1 FROM games g WHERE g.id = NEW.game_id
                     AND g.loser_id = NEW.respondent_id AND g.winner_id = NEW.opponent_id
                 ) THEN RAISE(ABORT, 'Answer participants do not match the game') END;
               END"""
        )
        await connection.execute("DELETE FROM sqlite_sequence WHERE name IN ('games', 'tournaments', 'group_sessions', 'countdowns', 'score_events', 'challenge_rounds', 'question_answers', 'choice_observations')")
        row = await (
            await connection.execute("SELECT count(*) FROM users WHERE telegram_id != -1")
        ).fetchone()
        counts["users_preserved"] = row[0]
        row = await (await connection.execute("SELECT count(*) FROM questions")).fetchone()
        counts["questions_preserved"] = row[0]
    return counts


def backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target_path = path.with_name(f"{path.name}.before-reset-{stamp}.bak")
    source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return target_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Settings().database_path)
    args = parser.parse_args()
    if not args.database.exists():
        raise SystemExit(f"Database not found: {args.database}")
    backup_path = backup(args.database)
    counts = asyncio.run(reset_history(Database(args.database)))
    print(f"Backup: {backup_path}")
    print(f"Reset complete: {counts}")


if __name__ == "__main__":
    main()
