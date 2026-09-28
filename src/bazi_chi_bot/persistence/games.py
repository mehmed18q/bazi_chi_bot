"""Game read queries."""

from __future__ import annotations

import time

import aiosqlite

from ..db import Database
from ..errors import GameNotFound
from ..models import Game
from ..persistence.mappers import _game_from_row


async def _locked_game(connection: aiosqlite.Connection, game_id: int) -> Game:
    row = await (
        await connection.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    ).fetchone()
    if row is None:
        raise GameNotFound
    return _game_from_row(row)


class GameRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get_game(self, game_id: int) -> Game:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute("SELECT * FROM games WHERE id = ?", (game_id,))
            ).fetchone()
        if row is None:
            raise GameNotFound
        return _game_from_row(row)

    async def get_game_by_token(self, invite_token: str) -> Game:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM games WHERE invite_token = ?", (invite_token,)
                )
            ).fetchone()
        if row is None:
            raise GameNotFound
        return _game_from_row(row)

    async def active_games(self, user_id: int) -> list[Game]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT * FROM games
                    WHERE (creator_id = ? OR player2_id = ?)
                      AND (status IN ('waiting', 'active', 'choice')
                           OR (status = 'finished' AND final_choice IS NOT NULL
                               AND final_response_approved IS NULL)
                           OR (game_type = 'truth_or_dare' AND challenge_response_text IS NOT NULL
                               AND challenge_approved IS NULL))
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (user_id, user_id),
                )
            ).fetchall()
        return [_game_from_row(row) for row in rows]

    async def latest_finished_game(self, user_id: int) -> Game | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM games
                    WHERE (creator_id = ? OR player2_id = ?) AND status = 'finished'
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """,
                    (user_id, user_id),
                )
            ).fetchone()
        return _game_from_row(row) if row else None

    async def game_message(self, game_id: int, user_id: int) -> tuple[int, int] | None:
        """Return the private chat/message pair used as a player's stable game card."""
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT chat_id, message_id FROM game_messages "
                    "WHERE game_id = ? AND user_id = ?",
                    (game_id, user_id),
                )
            ).fetchone()
        return (row["chat_id"], row["message_id"]) if row else None

    async def save_game_message(
        self, game_id: int, user_id: int, chat_id: int, message_id: int
    ) -> None:
        """Remember the latest editable game card for one player."""
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO game_messages (game_id, user_id, chat_id, message_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(game_id, user_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    message_id = excluded.message_id,
                    updated_at = excluded.updated_at
                """,
                (game_id, user_id, chat_id, message_id, int(time.time())),
            )
