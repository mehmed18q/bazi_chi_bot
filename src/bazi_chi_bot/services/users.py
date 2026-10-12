"""Transactional users use cases."""

from __future__ import annotations

import secrets
import time

import aiosqlite

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

    async def resolve_platform_user(self, platform: str, external_id: int) -> int:
        """Return the canonical shared-account ID for a platform identity.

        The first update from a new platform is registered immediately.  This
        lets the normal handlers continue using their existing integer user ID
        API while a later account-link flow can merge that temporary row into
        an already active account.
        """
        if platform not in {"telegram", "bale"}:
            raise ValueError("Unknown platform")
        domain_id = external_id if platform == "telegram" else -(external_id + 2)
        now = int(time.time())
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT user_id FROM user_platforms WHERE platform = ? AND external_id = ?",
                    (platform, external_id),
                )
            ).fetchone()
            if row is not None:
                return int(row["user_id"])
            await connection.execute(
                """
                INSERT OR IGNORE INTO user_platforms
                    (user_id, platform, external_id, linked_at)
                VALUES (?, ?, ?, ?)
                """,
                (domain_id, platform, external_id, now),
            )
        return domain_id

    async def platform_external_id(self, user_id: int, platform: str) -> int | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT external_id FROM user_platforms WHERE user_id = ? AND platform = ?",
                    (user_id, platform),
                )
            ).fetchone()
        return int(row["external_id"]) if row is not None else None

    async def platforms_for_user(self, user_id: int) -> tuple[tuple[str, int], ...]:
        """Return every messenger identity attached to a canonical account."""
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT platform, external_id
                    FROM user_platforms
                    WHERE user_id = ?
                    ORDER BY platform
                    """,
                    (user_id,),
                )
            ).fetchall()
        return tuple((str(row["platform"]), int(row["external_id"])) for row in rows)

    async def create_account_link_token(
        self, user_id: int, target_platform: str, *, ttl_seconds: int = 15 * 60
    ) -> str:
        if target_platform not in {"telegram", "bale"}:
            raise ValueError("Unknown platform")
        now = int(time.time())
        token = secrets.token_urlsafe(24)
        async with self.database.transaction() as connection:
            exists = await (
                await connection.execute(
                    "SELECT 1 FROM users WHERE telegram_id = ?", (user_id,)
                )
            ).fetchone()
            if exists is None:
                raise ValueError("User is not registered")
            await connection.execute(
                """
                DELETE FROM account_link_tokens
                WHERE user_id = ? AND target_platform = ?
                  AND (used_at IS NOT NULL OR expires_at <= ?)
                """,
                (user_id, target_platform, now),
            )
            await connection.execute(
                """
                INSERT INTO account_link_tokens
                    (token, user_id, target_platform, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token, user_id, target_platform, now, now + ttl_seconds),
            )
        return token

    async def link_platform_account(
        self,
        current_user_id: int,
        token: str,
        platform: str | None = None,
    ) -> int | None:
        """Consume a link token and merge the current platform into its owner."""
        if platform is not None and platform not in {"telegram", "bale"}:
            raise ValueError("Unknown platform")
        now = int(time.time())
        async with self.database.transaction() as connection:
            token_row = await (
                await connection.execute(
                    """
                    SELECT * FROM account_link_tokens
                    WHERE token = ? AND used_at IS NULL AND expires_at > ?
                    """,
                    (token, now),
                )
            ).fetchone()
            if token_row is None or (
                platform is not None and token_row["target_platform"] != platform
            ):
                return None
            owner_id = int(token_row["user_id"])
            if owner_id != current_user_id:
                await self._merge_users(connection, owner_id, current_user_id)
            await connection.execute(
                "UPDATE account_link_tokens SET used_at = ? WHERE token = ?",
                (now, token),
            )
            return owner_id

    @staticmethod
    async def _merge_users(connection, owner_id: int, duplicate_id: int) -> None:
        """Move references from a newly-created duplicate row to the owner.

        The duplicate is normally an empty row created by ``/start`` on the
        second messenger.  Foreign-key metadata keeps this list resilient as
        new game tables are added; a rare uniqueness collision is resolved by
        dropping the duplicate row's conflicting record.
        """
        if owner_id == duplicate_id:
            return
        owner = await (
            await connection.execute(
                "SELECT 1 FROM users WHERE telegram_id = ?", (owner_id,)
            )
        ).fetchone()
        duplicate = await (
            await connection.execute(
                "SELECT 1 FROM users WHERE telegram_id = ?", (duplicate_id,)
            )
        ).fetchone()
        if owner is None or duplicate is None:
            raise ValueError("Both linked users must be registered")

        owner_details = await (
            await connection.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (owner_id,)
            )
        ).fetchone()
        duplicate_details = await (
            await connection.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (duplicate_id,)
            )
        ).fetchone()
        # Account-level state belongs to the canonical row as well.  In
        # particular, linking an already-paid account must not remove its
        # activation when the other messenger was registered first.
        owner_custom_nickname = bool(owner_details["nickname_is_custom"])
        duplicate_custom_nickname = bool(duplicate_details["nickname_is_custom"])
        nickname = (
            owner_details["nickname"]
            if owner_custom_nickname
            else duplicate_details["nickname"]
            if duplicate_custom_nickname
            else owner_details["nickname"]
        )
        await connection.execute(
            """
            UPDATE users
            SET telegram_activated = CASE
                    WHEN telegram_activated = 1 OR ? = 1 THEN 1 ELSE 0 END,
                bale_activated = CASE
                    WHEN bale_activated = 1 OR ? = 1 THEN 1 ELSE 0 END,
                is_activated = CASE WHEN is_activated = 1 OR ? = 1 THEN 1 ELSE 0 END,
                activation_approved_at = COALESCE(activation_approved_at, ?),
                activation_approved_by = COALESCE(activation_approved_by, ?),
                pending_invite_token = COALESCE(pending_invite_token, ?),
                profile_photo_file_id = COALESCE(profile_photo_file_id, ?),
                nickname = ?,
                nickname_is_custom = CASE WHEN ? = 1 OR ? = 1 THEN 1 ELSE 0 END,
                referral_unlocked_at = CASE
                    WHEN referral_unlocked_at IS NULL THEN ?
                    WHEN ? IS NULL OR referral_unlocked_at >= ? THEN referral_unlocked_at
                    ELSE ? END,
                referral_unlock_notified_at = CASE
                    WHEN referral_unlock_notified_at IS NULL THEN ?
                    WHEN ? IS NULL OR referral_unlock_notified_at >= ?
                        THEN referral_unlock_notified_at
                    ELSE ? END,
                premium_revoked_by_admin = CASE
                    WHEN premium_revoked_by_admin = 1 OR ? = 1 THEN 1 ELSE 0 END,
                updated_at = MAX(updated_at, ?)
            WHERE telegram_id = ?
            """,
            (
                int(duplicate_details["telegram_activated"]),
                int(duplicate_details["bale_activated"]),
                int(duplicate_details["is_activated"]),
                duplicate_details["activation_approved_at"],
                duplicate_details["activation_approved_by"],
                duplicate_details["pending_invite_token"],
                duplicate_details["profile_photo_file_id"],
                nickname,
                int(owner_custom_nickname),
                int(duplicate_custom_nickname),
                duplicate_details["referral_unlocked_at"],
                duplicate_details["referral_unlocked_at"],
                owner_details["referral_unlocked_at"],
                duplicate_details["referral_unlocked_at"],
                duplicate_details["referral_unlock_notified_at"],
                duplicate_details["referral_unlock_notified_at"],
                owner_details["referral_unlock_notified_at"],
                duplicate_details["referral_unlock_notified_at"],
                int(duplicate_details["premium_revoked_by_admin"]),
                max(int(owner_details["updated_at"]), int(duplicate_details["updated_at"])),
                owner_id,
            ),
        )
        # Receipts keep a denormalized copy of the internal payment ID.  Once
        # the duplicate account is removed, point its historical receipts at
        # the canonical account's ID too.
        await connection.execute(
            """
            UPDATE payment_receipts
            SET user_internal_id = (SELECT id FROM users WHERE telegram_id = ?)
            WHERE user_telegram_id = ?
            """,
            (owner_id, duplicate_id),
        )
        await connection.execute(
            """UPDATE users
               SET referral_primary_platform = COALESCE(referral_primary_platform, ?),
                   referral_first_unlocked_at = CASE
                       WHEN referral_first_unlocked_at IS NULL THEN ?
                       WHEN ? IS NULL OR referral_first_unlocked_at >= ?
                           THEN referral_first_unlocked_at ELSE ? END,
                   referral_second_unlocked_at = CASE
                       WHEN referral_second_unlocked_at IS NULL THEN ?
                       WHEN ? IS NULL OR referral_second_unlocked_at >= ?
                           THEN referral_second_unlocked_at ELSE ? END
               WHERE telegram_id = ?""",
            (
                duplicate_details["referral_primary_platform"],
                duplicate_details["referral_first_unlocked_at"],
                duplicate_details["referral_first_unlocked_at"],
                owner_details["referral_first_unlocked_at"],
                duplicate_details["referral_first_unlocked_at"],
                duplicate_details["referral_second_unlocked_at"],
                duplicate_details["referral_second_unlocked_at"],
                owner_details["referral_second_unlocked_at"],
                duplicate_details["referral_second_unlocked_at"],
                owner_id,
            ),
        )

        # Totals are kept in user_stats while immutable score events are moved
        # below.  Adding the counters first preserves all-time statistics.
        await connection.execute(
            """
            UPDATE user_stats AS target
            SET games_played = target.games_played + source.games_played,
                wins = target.wins + source.wins,
                losses = target.losses + source.losses,
                correct_guesses = target.correct_guesses + source.correct_guesses,
                wrong_guesses = target.wrong_guesses + source.wrong_guesses,
                points_won = target.points_won + source.points_won
            FROM user_stats AS source
            WHERE target.telegram_id = ? AND source.telegram_id = ?
            """,
            (owner_id, duplicate_id),
        )
        await connection.execute("DELETE FROM user_stats WHERE telegram_id = ?", (duplicate_id,))

        tables = await (
            await connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ).fetchall()
        references: list[tuple[str, str]] = []
        for table_row in tables:
            table = str(table_row["name"])
            foreign_keys = await connection.execute(f"PRAGMA foreign_key_list({table})")
            for foreign_key in await foreign_keys.fetchall():
                if foreign_key["table"] == "users":
                    references.append((table, str(foreign_key["from"])))

        # Score history is intentionally immutable during normal operation.
        # Account linking is the one controlled maintenance operation that
        # needs to move those rows to the canonical user.  DDL is transactional
        # in SQLite, so the triggers are restored if anything below fails.
        await connection.execute("DROP TRIGGER IF EXISTS score_event_no_update")
        await connection.execute("DROP TRIGGER IF EXISTS score_event_no_delete")
        try:
            for table, column in references:
                if table == "user_stats":
                    continue
                try:
                    await connection.execute(
                        f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                        (owner_id, duplicate_id),
                    )
                except aiosqlite.IntegrityError:
                    # Composite/unique indexes can collide when both sides
                    # used the same feature.  Keep the owner-side record and
                    # discard only the duplicate-side rows for that feature.
                    await connection.execute(
                        f"DELETE FROM {table} WHERE {column} = ?", (duplicate_id,)
                    )
        finally:
            await connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS score_event_no_update BEFORE UPDATE ON score_events
                BEGIN
                    SELECT RAISE(ABORT, 'Score events are immutable');
                END
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS score_event_no_delete BEFORE DELETE ON score_events
                BEGIN
                    SELECT RAISE(ABORT, 'Score events are immutable');
                END
                """
            )

        await connection.execute(
            """
            DELETE FROM user_platforms
            WHERE user_id = ?
              AND EXISTS (
                  SELECT 1 FROM user_platforms owner_platform
                  WHERE owner_platform.user_id = ?
                    AND owner_platform.platform = user_platforms.platform
              )
            """,
            (duplicate_id, owner_id),
        )
        await connection.execute(
            "UPDATE user_platforms SET user_id = ? WHERE user_id = ?",
            (owner_id, duplicate_id),
        )
        await connection.execute(
            "UPDATE account_link_tokens SET user_id = ? WHERE user_id = ?",
            (owner_id, duplicate_id),
        )
        await connection.execute("DELETE FROM users WHERE telegram_id = ?", (duplicate_id,))

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
        if user.telegram_id != -1:
            platform = "bale" if user.telegram_id <= -2 else "telegram"
            external_id = -user.telegram_id - 2 if platform == "bale" else user.telegram_id
            await connection.execute(
                """
                INSERT OR IGNORE INTO user_platforms
                    (user_id, platform, external_id, linked_at)
                VALUES (?, ?, ?, ?)
                """,
                (user.telegram_id, platform, external_id, now),
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
                await connection.execute(
                    "SELECT * FROM users WHERE telegram_id != -1 ORDER BY updated_at DESC"
                )
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
