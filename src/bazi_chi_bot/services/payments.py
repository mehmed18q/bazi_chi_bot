"""Persistent activation payment settings, receipts, and admin review."""

from __future__ import annotations

import time

from ..db import Database
from ..models import PaymentReceipt, PaymentReceiptStatus, PaymentSettings, User
from ..persistence.mappers import _user_from_row
from .referrals import ReferralService


class PaymentService:
    def __init__(self, database: Database, referrals: ReferralService) -> None:
        self.database = database
        self.referrals = referrals

    async def settings(self) -> PaymentSettings:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute("SELECT * FROM payment_settings WHERE singleton_id = 1")
            ).fetchone()
        if row is None:
            raise RuntimeError("Payment settings are missing")
        return self._settings_from_row(row)

    async def update_settings(
        self,
        admin_id: int,
        *,
        base_amount_toman: int | None = None,
        card_number: str | None = None,
        card_holder: str | None = None,
    ) -> PaymentSettings:
        if base_amount_toman is not None and not 1 <= base_amount_toman <= 1_000_000_000:
            raise ValueError("Invalid base amount")
        if card_number is not None:
            card_number = "".join(character for character in card_number if character.isdigit())
            if not 8 <= len(card_number) <= 32:
                raise ValueError("Invalid card number")
        if card_holder is not None:
            card_holder = card_holder.strip()
            if not 2 <= len(card_holder) <= 100:
                raise ValueError("Invalid card holder")
        if all(value is None for value in (base_amount_toman, card_number, card_holder)):
            raise ValueError("No payment setting supplied")

        fields: list[str] = []
        values: list[object] = []
        if base_amount_toman is not None:
            fields.append("base_amount_toman = ?")
            values.append(base_amount_toman)
        if card_number is not None:
            fields.append("card_number = ?")
            values.append(card_number)
        if card_holder is not None:
            fields.append("card_holder = ?")
            values.append(card_holder)
        fields.extend(("updated_at = ?", "updated_by = ?"))
        values.extend((int(time.time()), admin_id))

        async with self.database.transaction() as connection:
            await connection.execute(
                f"UPDATE payment_settings SET {', '.join(fields)} WHERE singleton_id = 1",
                values,
            )
            row = await (
                await connection.execute("SELECT * FROM payment_settings WHERE singleton_id = 1")
            ).fetchone()
        return self._settings_from_row(row)

    async def expected_amount(self, user: User) -> int:
        if user.id is None:
            raise RuntimeError("User has no internal payment id")
        settings = await self.settings()
        return settings.base_amount_toman + user.id

    async def set_user_activation(self, user_id: int, admin_id: int, active: bool) -> User | None:
        now = int(time.time())
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE users
                SET is_activated = ?,
                    activation_approved_at = CASE WHEN ? = 1 THEN ? ELSE NULL END,
                    activation_approved_by = CASE WHEN ? = 1 THEN ? ELSE NULL END,
                    premium_revoked_by_admin = ?,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (int(active), int(active), now, int(active), admin_id,
                 int(not active), now, user_id),
            )
            if cursor.rowcount != 1:
                return None
            if active:
                await self.referrals.on_activation(connection, user_id, now)
            row = await (
                await connection.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
            ).fetchone()
        return _user_from_row(row)

    async def submit_receipt(
        self,
        user_id: int,
        *,
        receipt_text: str | None = None,
        telegram_file_id: str | None = None,
    ) -> PaymentReceipt:
        clean_text = receipt_text.strip() if receipt_text else None
        if clean_text and len(clean_text) > 3000:
            raise ValueError("Receipt text is too long")
        if telegram_file_id:
            receipt_type = "photo"
        elif clean_text:
            receipt_type = "text"
        else:
            raise ValueError("Receipt must contain text or a photo")

        now = int(time.time())
        async with self.database.transaction() as connection:
            user = await (
                await connection.execute(
                    "SELECT id, is_activated FROM users WHERE telegram_id = ?", (user_id,)
                )
            ).fetchone()
            if user is None or user["id"] is None:
                raise ValueError("Unknown user")
            if user["is_activated"]:
                raise ValueError("User is already activated")
            settings = await (
                await connection.execute(
                    "SELECT base_amount_toman FROM payment_settings WHERE singleton_id = 1"
                )
            ).fetchone()
            expected_amount = settings["base_amount_toman"] + user["id"]
            cursor = await connection.execute(
                """
                INSERT INTO payment_receipts (
                    user_telegram_id, user_internal_id, expected_amount_toman,
                    receipt_type, receipt_text, telegram_file_id, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    user["id"],
                    expected_amount,
                    receipt_type,
                    clean_text,
                    telegram_file_id,
                    now,
                ),
            )
            row = await (
                await connection.execute(
                    "SELECT * FROM payment_receipts WHERE id = ?", (cursor.lastrowid,)
                )
            ).fetchone()
        return self._receipt_from_row(row)

    async def receipt(self, receipt_id: int) -> PaymentReceipt | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,)
                )
            ).fetchone()
        return self._receipt_from_row(row) if row else None

    async def latest_pending_receipt(self, user_id: int) -> PaymentReceipt | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM payment_receipts
                    WHERE user_telegram_id = ? AND status = 'pending'
                    ORDER BY submitted_at DESC, id DESC LIMIT 1
                    """,
                    (user_id,),
                )
            ).fetchone()
        return self._receipt_from_row(row) if row else None

    async def review_receipt(
        self, receipt_id: int, admin_id: int, approved: bool
    ) -> tuple[PaymentReceipt | None, bool]:
        now = int(time.time())
        status = PaymentReceiptStatus.APPROVED if approved else PaymentReceiptStatus.REJECTED
        async with self.database.transaction() as connection:
            current = await (
                await connection.execute(
                    "SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,)
                )
            ).fetchone()
            if current is None:
                return None, False
            if current["status"] != PaymentReceiptStatus.PENDING.value:
                return self._receipt_from_row(current), False
            cursor = await connection.execute(
                """
                UPDATE payment_receipts
                SET status = ?, reviewed_at = ?, reviewed_by = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status.value, now, admin_id, receipt_id),
            )
            if cursor.rowcount != 1:
                return self._receipt_from_row(current), False
            if approved:
                await connection.execute(
                    """
                    UPDATE users
                    SET is_activated = 1, activation_approved_at = ?, activation_approved_by = ?,
                        premium_revoked_by_admin = 0
                    WHERE telegram_id = ?
                    """,
                    (now, admin_id, current["user_telegram_id"]),
                )
                await self.referrals.on_activation(
                    connection, current["user_telegram_id"], now
                )
                await connection.execute(
                    """
                    UPDATE payment_receipts
                    SET status = 'rejected', reviewed_at = ?, reviewed_by = ?
                    WHERE user_telegram_id = ? AND status = 'pending' AND id != ?
                    """,
                    (now, admin_id, current["user_telegram_id"], receipt_id),
                )
            row = await (
                await connection.execute(
                    "SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,)
                )
            ).fetchone()
        return self._receipt_from_row(row), True

    @staticmethod
    def _settings_from_row(row) -> PaymentSettings:
        return PaymentSettings(
            base_amount_toman=row["base_amount_toman"],
            card_number=row["card_number"],
            card_holder=row["card_holder"],
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
        )

    @staticmethod
    def _receipt_from_row(row) -> PaymentReceipt:
        return PaymentReceipt(
            id=row["id"],
            user_telegram_id=row["user_telegram_id"],
            user_internal_id=row["user_internal_id"],
            expected_amount_toman=row["expected_amount_toman"],
            receipt_type=row["receipt_type"],
            receipt_text=row["receipt_text"],
            telegram_file_id=row["telegram_file_id"],
            status=PaymentReceiptStatus(row["status"]),
            submitted_at=row["submitted_at"],
            reviewed_at=row["reviewed_at"],
            reviewed_by=row["reviewed_by"],
        )
