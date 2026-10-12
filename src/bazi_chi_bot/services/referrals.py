"""Persistent referral attribution, score awards, and premium unlocks."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import aiosqlite

from ..db import Database


@dataclass(frozen=True, slots=True)
class ReferralStats:
    invited: int
    activated: int
    required: int
    second_required: int | None = None
    primary_platform: str | None = None
    first_unlocked: bool = False
    second_unlocked: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.required - self.activated)

    @property
    def second_target(self) -> int:
        return self.second_required or (self.required * 3 + 1) // 2

    @property
    def remaining_second(self) -> int:
        return max(0, self.second_target - self.activated)


@dataclass(frozen=True, slots=True)
class ReferralNotice:
    invitee_id: int
    display_name: str
    kind: str
    occurred_at: int


class ReferralService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def set_primary_platform(self, user_id: int, platform: str) -> None:
        if platform not in {"telegram", "bale"}:
            raise ValueError("Unknown platform")
        async with self.database.transaction() as connection:
            await connection.execute(
                """UPDATE users SET referral_primary_platform = ?
                   WHERE telegram_id = ? AND referral_primary_platform IS NULL""",
                (platform, user_id),
            )

    async def code_for(self, user_id: int) -> str:
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT token FROM referral_codes WHERE user_id = ?", (user_id,)
                )
            ).fetchone()
            if row is not None:
                return row["token"]
            for _ in range(5):
                token = secrets.token_urlsafe(12)
                try:
                    await connection.execute(
                        "INSERT INTO referral_codes (user_id, token, created_at) VALUES (?, ?, ?)",
                        (user_id, token, int(time.time())),
                    )
                except aiosqlite.IntegrityError as error:
                    if "referral_codes.token" not in str(error):
                        raise
                else:
                    return token
        raise RuntimeError("Could not allocate a referral code")

    async def claim_code(self, invitee_id: int, token: str) -> bool:
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT user_id FROM referral_codes WHERE token = ?", (token,)
                )
            ).fetchone()
            if row is None:
                return False
            return await self._claim(connection, invitee_id, row["user_id"], "link")

    async def claim(self, invitee_id: int, inviter_id: int, source: str) -> bool:
        if source not in {"game", "group", "link"}:
            raise ValueError("Invalid referral source")
        async with self.database.transaction() as connection:
            return await self._claim(connection, invitee_id, inviter_id, source)

    @staticmethod
    async def _claim(
        connection: aiosqlite.Connection, invitee_id: int, inviter_id: int, source: str
    ) -> bool:
        if invitee_id == inviter_id or invitee_id == -1 or inviter_id == -1:
            return False
        invitee = await (
            await connection.execute(
                "SELECT is_activated FROM users WHERE telegram_id = ?", (invitee_id,)
            )
        ).fetchone()
        inviter = await (
            await connection.execute(
                "SELECT 1 FROM users WHERE telegram_id = ?", (inviter_id,)
            )
        ).fetchone()
        if invitee is None or invitee["is_activated"] or inviter is None:
            return False
        cycle = await (
            await connection.execute(
                """WITH RECURSIVE upstream(user_id) AS (
                     SELECT inviter_id FROM referrals WHERE invitee_id = ?
                     UNION ALL
                     SELECT r.inviter_id FROM referrals r
                     JOIN upstream u ON r.invitee_id = u.user_id
                   )
                   SELECT 1 FROM upstream WHERE user_id = ? LIMIT 1""",
                (inviter_id, invitee_id),
            )
        ).fetchone()
        if cycle is not None:
            return False
        now = int(time.time())
        cursor = await connection.execute(
            """INSERT OR IGNORE INTO referrals
               (invitee_id, inviter_id, source, joined_at) VALUES (?, ?, ?, ?)""",
            (invitee_id, inviter_id, source, now),
        )
        if cursor.rowcount != 1:
            return False
        await connection.execute(
            """INSERT INTO score_events
               (user_id, reason, referral_invitee_id, amount, created_at)
               VALUES (?, 'referral_join', ?, 1, ?)""",
            (inviter_id, invitee_id, now),
        )
        return True

    async def stats(self, user_id: int) -> ReferralStats:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """SELECT count(*) AS invited,
                              count(activated_at) AS activated
                       FROM referrals WHERE inviter_id = ?""",
                    (user_id,),
                )
            ).fetchone()
            setting = await (
                await connection.execute(
                    "SELECT required_activations FROM referral_settings WHERE singleton_id = 1"
                )
            ).fetchone()
            user = await (
                await connection.execute(
                    """SELECT referral_primary_platform, referral_first_unlocked_at,
                              referral_second_unlocked_at
                       FROM users WHERE telegram_id = ?""",
                    (user_id,),
                )
            ).fetchone()
        required = int(setting[0])
        return ReferralStats(
            row["invited"],
            row["activated"],
            required,
            (required * 3 + 1) // 2,
            user["referral_primary_platform"] if user else None,
            bool(user and user["referral_first_unlocked_at"]),
            bool(user and user["referral_second_unlocked_at"]),
        )

    async def required_activations(self) -> int:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT required_activations FROM referral_settings WHERE singleton_id = 1"
                )
            ).fetchone()
        return row[0]

    async def set_required_activations(self, admin_id: int, required: int) -> list[int]:
        if not 1 <= required <= 1000:
            raise ValueError("Referral target must be between 1 and 1000")
        now = int(time.time())
        async with self.database.transaction() as connection:
            await connection.execute(
                """UPDATE referral_settings
                   SET required_activations = ?, updated_at = ?, updated_by = ?
                   WHERE singleton_id = 1""",
                (required, now, admin_id),
            )
            rows = await (
                await connection.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id != -1"
                )
            ).fetchall()
            unlocked: list[int] = []
            for row in rows:
                count = await (
                    await connection.execute(
                        """SELECT count(*) FROM referrals
                           WHERE inviter_id = ? AND activated_at IS NOT NULL""",
                        (row["telegram_id"],),
                    )
                ).fetchone()
                changed = await self._unlock_thresholds(
                    connection, row["telegram_id"], int(count[0]), required, now
                )
                if changed:
                    unlocked.append(row["telegram_id"])
                    unlocked.extend(await self.on_activation(connection, row["telegram_id"], now))
        return unlocked

    @staticmethod
    async def _unlock_platform(
        connection: aiosqlite.Connection,
        user_id: int,
        platform: str,
        now: int,
        stage: int,
    ) -> bool:
        if platform not in {"telegram", "bale"}:
            return False
        row = await (
            await connection.execute(
                "SELECT telegram_activated, bale_activated, premium_revoked_by_admin "
                "FROM users WHERE telegram_id = ?",
                (user_id,),
            )
        ).fetchone()
        if row is None or row["premium_revoked_by_admin"]:
            return False
        changed = not bool(row[f"{platform}_activated"])
        cursor = await connection.execute(
            f"""UPDATE users SET {platform}_activated = 1,
                   is_activated = 1, activation_approved_at = ?,
                   activation_approved_by = NULL,
                   referral_unlocked_at = COALESCE(referral_unlocked_at, ?),
                   referral_{'first' if stage == 1 else 'second'}_unlocked_at = ?,
                   updated_at = ?
               WHERE telegram_id = ? AND premium_revoked_by_admin = 0""",
            (now, now, now, now, user_id),
        )
        return changed and cursor.rowcount == 1

    @staticmethod
    async def _unlock_thresholds(
        connection: aiosqlite.Connection,
        user_id: int,
        count: int,
        required: int,
        now: int,
    ) -> bool:
        row = await (
            await connection.execute(
                """SELECT telegram_activated, bale_activated, referral_primary_platform,
                          referral_first_unlocked_at, referral_second_unlocked_at,
                          premium_revoked_by_admin
                   FROM users WHERE telegram_id = ?""",
                (user_id,),
            )
        ).fetchone()
        if row is None or row["premium_revoked_by_admin"]:
            return False
        second_required = (required * 3 + 1) // 2
        primary = row["referral_primary_platform"]
        if primary not in {"telegram", "bale"}:
            primary = "telegram" if not row["telegram_activated"] else "bale"
            await connection.execute(
                "UPDATE users SET referral_primary_platform = ? WHERE telegram_id = ?",
                (primary, user_id),
            )
        changed = False
        if count >= required and row["referral_first_unlocked_at"] is None:
            changed = await ReferralService._unlock_platform(
                connection, user_id, primary, now, 1
            ) or changed
            await connection.execute(
                "UPDATE users SET referral_first_unlocked_at = COALESCE(referral_first_unlocked_at, ?) "
                "WHERE telegram_id = ?",
                (now, user_id),
            )
        if count >= second_required and row["referral_second_unlocked_at"] is None:
            second = "bale" if primary == "telegram" else "telegram"
            changed = await ReferralService._unlock_platform(
                connection, user_id, second, now, 2
            ) or changed
            await connection.execute(
                "UPDATE users SET referral_second_unlocked_at = COALESCE(referral_second_unlocked_at, ?) "
                "WHERE telegram_id = ?",
                (now, user_id),
            )
        return changed

    async def on_activation(
        self, connection: aiosqlite.Connection, user_id: int, now: int | None = None
    ) -> list[int]:
        """Award activation points and cascade threshold unlocks in one transaction."""
        current = int(time.time()) if now is None else now
        queue = [user_id]
        unlocked: list[int] = []
        required = await (
            await connection.execute(
                "SELECT required_activations FROM referral_settings WHERE singleton_id = 1"
            )
        ).fetchone()
        while queue:
            invitee_id = queue.pop(0)
            row = await (
                await connection.execute(
                    """SELECT inviter_id FROM referrals
                       WHERE invitee_id = ? AND activated_at IS NULL""",
                    (invitee_id,),
                )
            ).fetchone()
            if row is None:
                continue
            inviter_id = row["inviter_id"]
            await connection.execute(
                "UPDATE referrals SET activated_at = ? WHERE invitee_id = ?",
                (current, invitee_id),
            )
            await connection.execute(
                """INSERT INTO score_events
                   (user_id, reason, referral_invitee_id, amount, created_at)
                   VALUES (?, 'referral_activate', ?, 1, ?)""",
                (inviter_id, invitee_id, current),
            )
            count = await (
                await connection.execute(
                    """SELECT count(*) FROM referrals
                       WHERE inviter_id = ? AND activated_at IS NOT NULL""",
                    (inviter_id,),
                )
            ).fetchone()
            if await self._unlock_thresholds(
                connection, inviter_id, int(count[0]), int(required[0]), current
            ):
                unlocked.append(inviter_id)
                queue.append(inviter_id)
        return unlocked

    async def pending_notices(self, inviter_id: int) -> tuple[list[ReferralNotice], bool]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """SELECT r.invitee_id, u.display_name, r.joined_at, r.activated_at,
                              r.joined_notified_at, r.activated_notified_at
                       FROM referrals r JOIN users u ON u.telegram_id = r.invitee_id
                       WHERE r.inviter_id = ? AND
                           (r.joined_notified_at IS NULL OR
                            (r.activated_at IS NOT NULL AND r.activated_notified_at IS NULL))
                       ORDER BY r.joined_at, r.invitee_id LIMIT 100""",
                    (inviter_id,),
                )
            ).fetchall()
            unlock = await (
                await connection.execute(
                    """SELECT 1 FROM users WHERE telegram_id = ?
                       AND referral_unlocked_at IS NOT NULL
                       AND referral_unlock_notified_at IS NULL""",
                    (inviter_id,),
                )
            ).fetchone()
        notices = []
        for row in rows:
            if row["joined_notified_at"] is None:
                notices.append(ReferralNotice(row["invitee_id"], row["display_name"], "joined", row["joined_at"]))
            if row["activated_at"] is not None and row["activated_notified_at"] is None:
                notices.append(ReferralNotice(row["invitee_id"], row["display_name"], "activated", row["activated_at"]))
        notices.sort(
            key=lambda item: (item.occurred_at, item.invitee_id, item.kind != "joined")
        )
        return notices, bool(unlock)

    async def mark_notices(self, inviter_id: int, notices: list[ReferralNotice], unlock: bool) -> None:
        now = int(time.time())
        async with self.database.transaction() as connection:
            for notice in notices:
                field = "joined_notified_at" if notice.kind == "joined" else "activated_notified_at"
                await connection.execute(
                    f"UPDATE referrals SET {field} = ? WHERE inviter_id = ? AND invitee_id = ?",
                    (now, inviter_id, notice.invitee_id),
                )
            if unlock:
                await connection.execute(
                    """UPDATE users SET referral_unlock_notified_at = ?
                       WHERE telegram_id = ? AND referral_unlock_notified_at IS NULL""",
                    (now, inviter_id),
                )

    async def set_group_owner(self, chat_id: int, inviter_id: int) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO group_referral_owners (chat_id, inviter_id, added_at)
                   VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET
                   inviter_id = excluded.inviter_id, added_at = excluded.added_at""",
                (chat_id, inviter_id, int(time.time())),
            )

    async def group_owner(self, chat_id: int) -> int | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT inviter_id FROM group_referral_owners WHERE chat_id = ?", (chat_id,)
                )
            ).fetchone()
        return row[0] if row else None

    async def remove_group_owner(self, chat_id: int) -> None:
        async with self.database.transaction() as connection:
            await connection.execute("DELETE FROM group_referral_owners WHERE chat_id = ?", (chat_id,))
