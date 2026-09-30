"""Transactional users use cases."""

from __future__ import annotations

import time

from ..db import Database
from ..leaderboard import leaderboard_period_start_epoch
from ..models import LeaderboardEntry, Stats, User
from ..persistence.mappers import _user_from_row
from ..persistence.scores import award_login_bonus


class UserService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def save_user(self, user: User) -> None:
        now = int(time.time())
        async with self.database.transaction() as connection:
            await self._save_user(connection, user, now)

    async def register_entry(self, user: User, *, activation_exempt: bool = False) -> None:
        """Save a user and award the monthly login point only after activation."""
        now = int(time.time())
        period_start = leaderboard_period_start_epoch(now)
        async with self.database.transaction() as connection:
            await self._save_user(connection, user, now)
            row = await (
                await connection.execute(
                    "SELECT is_activated FROM users WHERE telegram_id = ?",
                    (user.telegram_id,),
                )
            ).fetchone()
            if activation_exempt or bool(row["is_activated"]):
                await award_login_bonus(connection, user.telegram_id, period_start)

    async def set_pending_invite(self, telegram_id: int, token: str | None) -> None:
        if token is not None and (not token or len(token) > 100):
            raise ValueError("Invalid invite token")
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE users SET pending_invite_token = ?, updated_at = ? WHERE telegram_id = ?",
                (token, int(time.time()), telegram_id),
            )

    async def set_nickname(self, telegram_id: int, nickname: str | None) -> User | None:
        custom = nickname is not None
        cleaned = " ".join(nickname.split()) if nickname is not None else None
        if cleaned is not None and not 1 <= len(cleaned) <= 40:
            raise ValueError("Nickname must be between 1 and 40 characters")
        now = int(time.time())
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE users
                SET nickname = CASE WHEN ? = 1 THEN ? ELSE display_name END,
                    nickname_is_custom = ?, updated_at = ?
                WHERE telegram_id = ?
                """,
                (int(custom), cleaned, int(custom), now, telegram_id),
            )
            if cursor.rowcount != 1:
                return None
            row = await (
                await connection.execute(
                    "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
                )
            ).fetchone()
        return _user_from_row(row)

    @staticmethod
    async def _save_user(connection, user: User, now: int) -> None:
        telegram_display_name = user.telegram_display_name or user.display_name
        await connection.execute(
            """
            INSERT INTO users (
                id, telegram_id, username, first_name, last_name, display_name,
                nickname, created_at, updated_at
            ) VALUES (
                (SELECT COALESCE(MAX(id), 0) + 1 FROM users),
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                display_name = excluded.display_name,
                nickname = CASE
                    WHEN users.nickname_is_custom = 1 THEN users.nickname
                    ELSE excluded.display_name
                END,
                updated_at = excluded.updated_at
            """,
            (
                user.telegram_id,
                user.username,
                user.first_name,
                user.last_name,
                telegram_display_name,
                telegram_display_name,
                now,
                now,
            ),
        )
        await connection.execute(
            "INSERT OR IGNORE INTO user_stats (telegram_id) VALUES (?)",
            (user.telegram_id,),
        )

    async def get_user(self, telegram_id: int) -> User | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
                )
            ).fetchone()
        return _user_from_row(row) if row else None

    async def set_profile_photo(self, telegram_id: int, file_id: str | None) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE users SET profile_photo_file_id = ?, updated_at = ? WHERE telegram_id = ?",
                (file_id, int(time.time()), telegram_id),
            )

    async def get_stats(self, telegram_id: int) -> Stats:
        period_start = leaderboard_period_start_epoch()
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT
                        telegram_id, games_played, wins, losses,
                        correct_guesses, wrong_guesses,
                        COALESCE(
                            (SELECT SUM(amount) FROM score_events
                             WHERE user_id = user_stats.telegram_id AND created_at >= ?),
                            0
                        ) AS points_won
                    FROM user_stats
                    WHERE telegram_id = ?
                    """,
                    (period_start, telegram_id),
                )
            ).fetchone()
        if row is None:
            return Stats(telegram_id, 0, 0, 0, 0, 0, 0)
        return Stats(
            telegram_id=row["telegram_id"],
            games_played=row["games_played"],
            wins=row["wins"],
            losses=row["losses"],
            correct_guesses=row["correct_guesses"],
            wrong_guesses=row["wrong_guesses"],
            points_won=row["points_won"],
        )

    async def all_users(self) -> list[User]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute("SELECT * FROM users WHERE telegram_id != -1 ORDER BY updated_at DESC")
            ).fetchall()
        return [_user_from_row(row) for row in rows]

    async def leaderboard(self, limit: int = 3) -> list[LeaderboardEntry]:
        """Return the highest-scoring registered players in stable rank order."""
        safe_limit = max(0, int(limit))
        if safe_limit == 0:
            return []
        period_start = leaderboard_period_start_epoch()
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    WITH monthly_points AS (
                        SELECT user_id, SUM(amount) AS points_won
                        FROM score_events
                        WHERE created_at >= ?
                        GROUP BY user_id
                    ), ranked AS (
                        SELECT
                            u.telegram_id,
                            COALESCE(u.nickname, u.display_name) AS display_name,
                            COALESCE(p.points_won, 0) AS points_won,
                            COALESCE(s.wins, 0) AS wins,
                            COALESCE(s.games_played, 0) AS games_played,
                            ROW_NUMBER() OVER (
                                ORDER BY
                                    COALESCE(p.points_won, 0) DESC,
                                    COALESCE(s.wins, 0) DESC,
                                    COALESCE(s.games_played, 0) DESC,
                                    u.telegram_id ASC
                            ) AS rank
                        FROM users AS u
                        LEFT JOIN user_stats AS s ON s.telegram_id = u.telegram_id
                        LEFT JOIN monthly_points AS p ON p.user_id = u.telegram_id
                        WHERE u.telegram_id != -1
                    )
                    SELECT rank, telegram_id, display_name, points_won, wins, games_played
                    FROM ranked
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (period_start, safe_limit),
                )
            ).fetchall()
        return [self._leaderboard_entry(row) for row in rows]

    async def leaderboard_position(self, telegram_id: int) -> LeaderboardEntry | None:
        """Return a player's current rank, or ``None`` if they are not registered."""
        period_start = leaderboard_period_start_epoch()
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    WITH monthly_points AS (
                        SELECT user_id, SUM(amount) AS points_won
                        FROM score_events
                        WHERE created_at >= ?
                        GROUP BY user_id
                    ), ranked AS (
                        SELECT
                            u.telegram_id,
                            COALESCE(u.nickname, u.display_name) AS display_name,
                            COALESCE(p.points_won, 0) AS points_won,
                            COALESCE(s.wins, 0) AS wins,
                            COALESCE(s.games_played, 0) AS games_played,
                            ROW_NUMBER() OVER (
                                ORDER BY
                                    COALESCE(p.points_won, 0) DESC,
                                    COALESCE(s.wins, 0) DESC,
                                    COALESCE(s.games_played, 0) DESC,
                                    u.telegram_id ASC
                            ) AS rank
                        FROM users AS u
                        LEFT JOIN user_stats AS s ON s.telegram_id = u.telegram_id
                        LEFT JOIN monthly_points AS p ON p.user_id = u.telegram_id
                        WHERE u.telegram_id != -1
                    )
                    SELECT rank, telegram_id, display_name, points_won, wins, games_played
                    FROM ranked
                    WHERE telegram_id = ?
                    """,
                    (period_start, telegram_id),
                )
            ).fetchone()
        return self._leaderboard_entry(row) if row else None

    async def all_time_leaderboard(self, limit: int = 3) -> list[LeaderboardEntry]:
        """Return the highest-scoring players across the complete score history."""
        safe_limit = max(0, int(limit))
        if safe_limit == 0:
            return []
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            u.telegram_id,
                            COALESCE(u.nickname, u.display_name) AS display_name,
                            COALESCE(s.points_won, 0) AS points_won,
                            COALESCE(s.wins, 0) AS wins,
                            COALESCE(s.games_played, 0) AS games_played,
                            ROW_NUMBER() OVER (
                                ORDER BY
                                    COALESCE(s.points_won, 0) DESC,
                                    COALESCE(s.wins, 0) DESC,
                                    COALESCE(s.games_played, 0) DESC,
                                    u.telegram_id ASC
                            ) AS rank
                        FROM users AS u
                        LEFT JOIN user_stats AS s ON s.telegram_id = u.telegram_id
                        WHERE u.telegram_id != -1
                    )
                    SELECT rank, telegram_id, display_name, points_won, wins, games_played
                    FROM ranked
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (safe_limit,),
                )
            ).fetchall()
        return [self._leaderboard_entry(row) for row in rows]

    async def all_time_leaderboard_position(self, telegram_id: int) -> LeaderboardEntry | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            u.telegram_id,
                            COALESCE(u.nickname, u.display_name) AS display_name,
                            COALESCE(s.points_won, 0) AS points_won,
                            COALESCE(s.wins, 0) AS wins,
                            COALESCE(s.games_played, 0) AS games_played,
                            ROW_NUMBER() OVER (
                                ORDER BY
                                    COALESCE(s.points_won, 0) DESC,
                                    COALESCE(s.wins, 0) DESC,
                                    COALESCE(s.games_played, 0) DESC,
                                    u.telegram_id ASC
                            ) AS rank
                        FROM users AS u
                        LEFT JOIN user_stats AS s ON s.telegram_id = u.telegram_id
                        WHERE u.telegram_id != -1
                    )
                    SELECT rank, telegram_id, display_name, points_won, wins, games_played
                    FROM ranked
                    WHERE telegram_id = ?
                    """,
                    (telegram_id,),
                )
            ).fetchone()
        return self._leaderboard_entry(row) if row else None

    async def get_leaderboard(self, limit: int = 3) -> list[LeaderboardEntry]:
        return await self.leaderboard(limit)

    async def get_leaderboard_position(self, telegram_id: int) -> LeaderboardEntry | None:
        return await self.leaderboard_position(telegram_id)

    async def get_all_time_leaderboard(self, limit: int = 3) -> list[LeaderboardEntry]:
        return await self.all_time_leaderboard(limit)

    async def get_all_time_leaderboard_position(self, telegram_id: int) -> LeaderboardEntry | None:
        return await self.all_time_leaderboard_position(telegram_id)

    @staticmethod
    def _leaderboard_entry(row) -> LeaderboardEntry:
        return LeaderboardEntry(
            rank=row["rank"],
            telegram_id=row["telegram_id"],
            display_name=row["display_name"],
            points_won=row["points_won"],
            wins=row["wins"],
            games_played=row["games_played"],
        )
