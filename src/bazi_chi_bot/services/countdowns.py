"""Persistent countdown use cases."""

import time

import aiosqlite

from ..daily_schedule import next_reminder_at
from ..db import Database
from ..errors import DailyChallengeRequiresActivation
from ..models import Countdown, CountdownStatus, User
from ..persistence.mappers import _user_from_row
from ..scheduling import next_delivery_at


class CountdownError(Exception):
    """Base class for expected countdown failures."""


class CountdownTargetNotFound(CountdownError):
    pass


class CountdownTimeInPast(CountdownError):
    pass


class CountdownNotFound(CountdownError):
    pass


def _countdown_from_row(row: aiosqlite.Row) -> Countdown:
    return Countdown(
        id=row["id"],
        creator_id=row["creator_id"],
        target_user_id=row["target_user_id"],
        target_at=row["target_at"],
        next_run_at=row["next_run_at"],
        status=CountdownStatus(row["status"]),
        created_at=row["created_at"],
        last_sent_at=row["last_sent_at"],
        completed_at=row["completed_at"],
        kind=row["kind"],
    )


class CountdownService:
    def __init__(
        self, database: Database, *, activation_exempt_ids: frozenset[int] = frozenset()
    ) -> None:
        self.database = database
        self.activation_exempt_ids = activation_exempt_ids

    def can_receive_daily_reminder(self, user: User | None) -> bool:
        return user is not None and (
            user.is_activated or user.telegram_id in self.activation_exempt_ids
        )

    async def resolve_user(self, value: str) -> User | None:
        identifier = value.strip()
        async with self.database.connect() as connection:
            if identifier.startswith("@"):
                row = await (
                    await connection.execute(
                        """
                        SELECT * FROM users
                        WHERE username = ? COLLATE NOCASE
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        (identifier[1:],),
                    )
                ).fetchone()
            else:
                try:
                    telegram_id = int(identifier)
                except ValueError:
                    return None
                row = await (
                    await connection.execute(
                        "SELECT * FROM users WHERE telegram_id = ? AND telegram_id != -1",
                        (telegram_id,),
                    )
                ).fetchone()
        if row is None:
            return None
        return _user_from_row(row)

    async def all_users(self) -> list[User]:
        """Return only people who have previously started/interacted with the bot."""
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT * FROM users WHERE telegram_id != -1
                    ORDER BY COALESCE(nickname, display_name) COLLATE NOCASE, telegram_id
                    """
                )
            ).fetchall()
        return [_user_from_row(row) for row in rows]

    async def create(
        self,
        creator_id: int,
        target_user_id: int,
        target_at: int,
        *,
        now: int | None = None,
    ) -> tuple[Countdown, bool]:
        current_time = int(time.time()) if now is None else now
        if target_at <= current_time:
            raise CountdownTimeInPast

        async with self.database.transaction() as connection:
            target = await (
                await connection.execute(
                    "SELECT 1 FROM users WHERE telegram_id = ? AND telegram_id != -1",
                    (target_user_id,),
                )
            ).fetchone()
            if target is None:
                raise CountdownTargetNotFound

            cursor = await connection.execute(
                """
                UPDATE countdowns
                SET status = 'cancelled'
                WHERE target_user_id = ? AND status = 'active' AND kind = 'standard'
                """,
                (target_user_id,),
            )
            replaced_existing = cursor.rowcount > 0
            inserted = await connection.execute(
                """
                INSERT INTO countdowns (
                    creator_id, target_user_id, target_at, next_run_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (creator_id, target_user_id, target_at, current_time, current_time),
            )
            row = await (
                await connection.execute(
                    "SELECT * FROM countdowns WHERE id = ?", (inserted.lastrowid,)
                )
            ).fetchone()
        return _countdown_from_row(row), replaced_existing

    async def next_due(self) -> Countdown | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM countdowns
                    WHERE status = 'active'
                    ORDER BY next_run_at, id
                    LIMIT 1
                    """
                )
            ).fetchone()
        return _countdown_from_row(row) if row else None

    async def make_all_active_due(self, *, now: int | None = None) -> int:
        """Make every active countdown send once when the bot starts."""
        current_time = int(time.time()) if now is None else now
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE countdowns SET next_run_at = ?
                WHERE status = 'active' AND kind = 'standard'
                """,
                (current_time,),
            )
        return cursor.rowcount

    async def record_sent(self, countdown_id: int, sent_at: int, target_at: int) -> None:
        next_run = next_delivery_at(sent_at, target_at)
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE countdowns
                SET last_sent_at = ?, next_run_at = ?
                WHERE id = ? AND status = 'active' AND kind = 'standard'
                """,
                (sent_at, next_run, countdown_id),
            )

    async def retry_at(self, countdown_id: int, retry_at: int) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE countdowns SET next_run_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (retry_at, countdown_id),
            )

    async def complete(self, countdown_id: int, completed_at: int) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE countdowns
                SET status = 'completed', last_sent_at = ?, completed_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (completed_at, completed_at, countdown_id),
            )

    async def cancel_for_target(self, target_user_id: int) -> bool:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE countdowns SET status = 'cancelled'
                WHERE target_user_id = ? AND status = 'active' AND kind = 'standard'
                """,
                (target_user_id,),
            )
        return cursor.rowcount > 0

    async def active_created_by(self, creator_id: int) -> list[Countdown]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT * FROM countdowns
                    WHERE creator_id = ? AND status = 'active' AND kind = 'standard'
                    ORDER BY target_at, id
                    """,
                    (creator_id,),
                )
            ).fetchall()
        return [_countdown_from_row(row) for row in rows]

    async def daily_reminder(self, user_id: int) -> Countdown | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM countdowns
                    WHERE target_user_id = ? AND kind = 'daily_reminder' AND status = 'active'
                    """,
                    (user_id,),
                )
            ).fetchone()
        return _countdown_from_row(row) if row else None

    async def subscribe_daily_reminder(self, user_id: int, *, now: int | None = None) -> Countdown:
        current = int(time.time()) if now is None else now
        target = next_reminder_at(current)
        async with self.database.transaction() as connection:
            user = await (
                await connection.execute(
                    "SELECT is_activated FROM users WHERE telegram_id = ?", (user_id,)
                )
            ).fetchone()
            if user is None or (
                not user["is_activated"] and user_id not in self.activation_exempt_ids
            ):
                raise DailyChallengeRequiresActivation
            await connection.execute(
                """
                INSERT OR IGNORE INTO countdowns (
                    creator_id, target_user_id, target_at, next_run_at, status,
                    created_at, kind
                ) VALUES (?, ?, ?, ?, 'active', ?, 'daily_reminder')
                """,
                (user_id, user_id, target, target, current),
            )
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM countdowns
                    WHERE target_user_id = ? AND kind = 'daily_reminder' AND status = 'active'
                    """,
                    (user_id,),
                )
            ).fetchone()
        return _countdown_from_row(row)

    async def cancel_daily_reminder(self, user_id: int) -> bool:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE countdowns SET status = 'cancelled'
                WHERE target_user_id = ? AND kind = 'daily_reminder' AND status = 'active'
                """,
                (user_id,),
            )
        return cursor.rowcount > 0

    async def record_daily_reminder_sent(self, countdown_id: int, sent_at: int) -> None:
        target = next_reminder_at(sent_at)
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE countdowns
                SET last_sent_at = ?, target_at = ?, next_run_at = ?
                WHERE id = ? AND status = 'active' AND kind = 'daily_reminder'
                """,
                (sent_at, target, target, countdown_id),
            )
