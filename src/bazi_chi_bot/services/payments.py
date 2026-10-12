"""Persistent activation payment settings, receipts, and admin review."""

from __future__ import annotations

import secrets
import time

import aiosqlite

from ..db import Database
from ..models import PaymentReceipt, PaymentReceiptStatus, PaymentSettings, User
from ..persistence.mappers import _user_from_row
from .referrals import ReferralService

PLATFORMS = ("telegram", "bale")


def _targets(platform: str, scope: str) -> tuple[str, ...]:
    if platform not in PLATFORMS:
        raise ValueError("Unknown platform")
    if scope == "current":
        scope = platform
    if scope not in {*PLATFORMS, "both"}:
        raise ValueError("Unknown activation scope")
    return (platform,) if scope == platform else PLATFORMS if scope == "both" else (scope,)


def _stored_scope(platform: str, scope: str) -> str:
    if scope == "current":
        return platform
    if scope == platform or scope == "both":
        return scope
    raise ValueError("Unknown activation scope")


def _activation_amount(base_amount: int, internal_id: int, target_count: int) -> int:
    """Calculate the requested activation price in toman.

    The existing per-user internal-ID suffix remains part of the single
    platform price.  Unlocking two platforms at once costs one and a half
    times that price, rounded up for odd values.
    """
    unit = base_amount + internal_id
    return unit if target_count <= 1 else (unit * 3 + 1) // 2


def activation_amount(base_amount: int, internal_id: int, *, both: bool = False) -> int:
    """Public pricing helper for UI and integrations."""
    return _activation_amount(base_amount, internal_id, 2 if both else 1)


class PaymentService:
    def __init__(
        self,
        database: Database,
        referrals: ReferralService,
        *,
        bale_provider_token: str | None = None,
    ) -> None:
        self.database = database
        self.referrals = referrals
        self.bale_provider_token = bale_provider_token.strip() if bale_provider_token else None

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
        bale_payment_enabled: bool | None = None,
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
        if bale_payment_enabled and not self.bale_provider_token:
            raise ValueError("Bale payment provider token is not configured")
        if all(
            value is None
            for value in (base_amount_toman, card_number, card_holder, bale_payment_enabled)
        ):
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
        if bale_payment_enabled is not None:
            fields.append("bale_payment_enabled = ?")
            values.append(int(bale_payment_enabled))
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

    async def expected_amount(
        self, user: User, *, platform: str = "telegram", scope: str = "current"
    ) -> int:
        if user.id is None:
            raise RuntimeError("User has no internal payment id")
        settings = await self.settings()
        requested = _targets(platform, scope)
        needed = tuple(name for name in requested if not user.is_platform_activated(name))
        if not needed:
            raise ValueError("User is already activated")
        return _activation_amount(settings.base_amount_toman, user.id, len(needed))

    @staticmethod
    def _needed_targets(row, platform: str, scope: str) -> tuple[str, ...]:
        requested = _targets(platform, scope)
        return tuple(
            name
            for name in requested
            if not bool(row[f"{name}_activated"])
        )

    @staticmethod
    async def _activate_targets(
        connection: aiosqlite.Connection,
        user_id: int,
        targets: tuple[str, ...],
        now: int,
        admin_id: int | None,
    ) -> None:
        if not targets:
            return
        assignments = [f"{platform}_activated = 1" for platform in targets]
        assignments.extend(
            (
                "is_activated = 1",
                "activation_approved_at = ?",
                "activation_approved_by = ?",
                "premium_revoked_by_admin = 0",
                "updated_at = ?",
            )
        )
        values: list[object] = [now, admin_id, now]
        values.append(user_id)
        await connection.execute(
            f"UPDATE users SET {', '.join(assignments)} WHERE telegram_id = ?",
            values,
        )

    async def bale_payment_available(self) -> bool:
        settings = await self.settings()
        return bool(self.bale_provider_token and settings.bale_payment_enabled)

    async def bale_payment_token(self) -> str | None:
        return self.bale_provider_token if await self.bale_payment_available() else None

    async def create_bale_payment_intent(
        self, user_id: int, *, scope: str = "current"
    ) -> tuple[str, int]:
        """Create a one-time invoice payload and return it with its rial amount."""
        if not await self.bale_payment_available():
            raise ValueError("Bale wallet payment is disabled")
        stored_scope = _stored_scope("bale", scope)
        now = int(time.time())
        payload = f"bale_subscription:{secrets.token_urlsafe(18)}"
        async with self.database.transaction() as connection:
            user = await (
                await connection.execute(
                    "SELECT id, telegram_activated, bale_activated FROM users WHERE telegram_id = ?",
                    (user_id,),
                )
            ).fetchone()
            if user is None or user["id"] is None:
                raise ValueError("Unknown user")
            targets = self._needed_targets(user, "bale", stored_scope)
            if not targets:
                raise ValueError("User is already activated")
            settings = await (
                await connection.execute(
                    "SELECT base_amount_toman FROM payment_settings WHERE singleton_id = 1"
                )
            ).fetchone()
            amount_irr = _activation_amount(
                int(settings["base_amount_toman"]), int(user["id"]), len(targets)
            ) * 10
            await connection.execute(
                """
                INSERT INTO native_payment_transactions
                    (platform, user_id, payload, amount_irr, currency, activation_scope, created_at)
                VALUES ('bale', ?, ?, ?, 'IRR', ?, ?)
                """,
                (user_id, payload, amount_irr, stored_scope, now),
            )
        return payload, amount_irr

    async def validate_bale_pre_checkout(
        self,
        *,
        user_id: int,
        payload: str,
        amount_irr: int,
        currency: str,
    ) -> bool:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT user_id, amount_irr, currency, status
                    FROM native_payment_transactions
                    WHERE platform = 'bale' AND payload = ?
                    """,
                    (payload,),
                )
            ).fetchone()
        return bool(
            row
            and row["status"] == "pending"
            and int(row["user_id"]) == user_id
            and int(row["amount_irr"]) == amount_irr
            and row["currency"] == currency == "IRR"
        )

    async def complete_bale_payment(
        self,
        *,
        user_id: int,
        payload: str,
        amount_irr: int,
        currency: str,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str,
    ) -> tuple[User | None, bool]:
        """Finalize a Bale payment exactly once and activate the canonical user."""
        now = int(time.time())
        async with self.database.transaction() as connection:
            current = await (
                await connection.execute(
                    """
                    SELECT * FROM native_payment_transactions
                    WHERE platform = 'bale' AND payload = ?
                    """,
                    (payload,),
                )
            ).fetchone()
            if current is None or int(current["user_id"]) != user_id:
                return None, False
            if current["status"] == "paid":
                row = await (
                    await connection.execute(
                        "SELECT * FROM users WHERE telegram_id = ?", (user_id,)
                    )
                ).fetchone()
                return (_user_from_row(row) if row else None), False
            if (
                current["status"] != "pending"
                or int(current["amount_irr"]) != amount_irr
                or current["currency"] != currency
                or currency != "IRR"
            ):
                return None, False
            try:
                cursor = await connection.execute(
                    """
                    UPDATE native_payment_transactions
                    SET status = 'paid', telegram_payment_charge_id = ?,
                        provider_payment_charge_id = ?, paid_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (telegram_payment_charge_id, provider_payment_charge_id, now, current["id"]),
                )
            except aiosqlite.IntegrityError:
                # A charge ID already recorded for another payload is a
                # duplicate/replayed provider update; do not activate anyone.
                return None, False
            if cursor.rowcount != 1:
                return None, False
            user_row = await (
                await connection.execute(
                    "SELECT telegram_activated, bale_activated FROM users WHERE telegram_id = ?",
                    (user_id,),
                )
            ).fetchone()
            if user_row is None:
                return None, False
            targets = self._needed_targets(user_row, "bale", current["activation_scope"])
            await self._activate_targets(connection, user_id, targets, now, None)
            await self.referrals.on_activation(connection, user_id, now)
            row = await (
                await connection.execute(
                    "SELECT * FROM users WHERE telegram_id = ?", (user_id,)
                )
            ).fetchone()
        return (_user_from_row(row) if row else None), True

    async def set_user_activation(self, user_id: int, admin_id: int, active: bool) -> User | None:
        now = int(time.time())
        async with self.database.transaction() as connection:
            if active:
                cursor = await connection.execute(
                    "SELECT telegram_activated, bale_activated FROM users WHERE telegram_id = ?",
                    (user_id,),
                )
                current = await cursor.fetchone()
                if current is None:
                    return None
                await self._activate_targets(connection, user_id, PLATFORMS, now, admin_id)
                changed = True
            else:
                cursor = await connection.execute(
                    """
                    UPDATE users
                    SET is_activated = 0, telegram_activated = 0, bale_activated = 0,
                        activation_approved_at = NULL, activation_approved_by = NULL,
                        premium_revoked_by_admin = 1, updated_at = ?
                    WHERE telegram_id = ?
                    """,
                    (now, user_id),
                )
                changed = cursor.rowcount == 1
            if not changed:
                return None
            if active:
                await self.referrals.on_activation(connection, user_id, now)
            row = await (
                await connection.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
            ).fetchone()
        return _user_from_row(row)

    async def set_platform_activation(
        self, user_id: int, platform: str, admin_id: int, active: bool
    ) -> User | None:
        """Change one platform's access while preserving the other platform."""
        if platform not in PLATFORMS:
            raise ValueError("Unknown platform")
        now = int(time.time())
        async with self.database.transaction() as connection:
            exists = await (
                await connection.execute(
                    "SELECT 1 FROM users WHERE telegram_id = ?", (user_id,)
                )
            ).fetchone()
            if exists is None:
                return None
            if active:
                await self._activate_targets(connection, user_id, (platform,), now, admin_id)
                await self.referrals.on_activation(connection, user_id, now)
            else:
                other = "bale" if platform == "telegram" else "telegram"
                await connection.execute(
                    f"""UPDATE users SET {platform}_activated = 0,
                               is_activated = CASE WHEN {other}_activated = 1 THEN 1 ELSE 0 END,
                               activation_approved_at = CASE WHEN {other}_activated = 1 THEN activation_approved_at ELSE NULL END,
                               activation_approved_by = CASE WHEN {other}_activated = 1 THEN activation_approved_by ELSE NULL END,
                               premium_revoked_by_admin = CASE WHEN {other}_activated = 1 THEN premium_revoked_by_admin ELSE 1 END,
                               updated_at = ?
                           WHERE telegram_id = ?""",
                    (now, user_id),
                )
            row = await (
                await connection.execute(
                    "SELECT * FROM users WHERE telegram_id = ?", (user_id,)
                )
            ).fetchone()
        return _user_from_row(row) if row else None

    async def submit_receipt(
        self,
        user_id: int,
        *,
        receipt_text: str | None = None,
        telegram_file_id: str | None = None,
        platform: str = "telegram",
        scope: str = "current",
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
        stored_scope = _stored_scope(platform, scope)

        now = int(time.time())
        async with self.database.transaction() as connection:
            user = await (
                await connection.execute(
                    "SELECT id, telegram_activated, bale_activated FROM users WHERE telegram_id = ?",
                    (user_id,),
                )
            ).fetchone()
            if user is None or user["id"] is None:
                raise ValueError("Unknown user")
            targets = self._needed_targets(user, platform, stored_scope)
            if not targets:
                raise ValueError("User is already activated")
            settings = await (
                await connection.execute(
                    "SELECT base_amount_toman FROM payment_settings WHERE singleton_id = 1"
                )
            ).fetchone()
            expected_amount = _activation_amount(
                int(settings["base_amount_toman"]), int(user["id"]), len(targets)
            )
            cursor = await connection.execute(
                """
                INSERT INTO payment_receipts (
                    user_telegram_id, user_internal_id, expected_amount_toman,
                    receipt_type, receipt_text, telegram_file_id, activation_scope,
                    platform, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    user["id"],
                    expected_amount,
                    receipt_type,
                    clean_text,
                    telegram_file_id,
                    stored_scope,
                    platform,
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
                user_row = await (
                    await connection.execute(
                        "SELECT telegram_activated, bale_activated FROM users WHERE telegram_id = ?",
                        (current["user_telegram_id"],),
                    )
                ).fetchone()
                if user_row is None:
                    return None, False
                targets = self._needed_targets(
                    user_row, current["platform"], current["activation_scope"]
                )
                await self._activate_targets(
                    connection, current["user_telegram_id"], targets, now, admin_id
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

    def _settings_from_row(self, row) -> PaymentSettings:
        return PaymentSettings(
            base_amount_toman=row["base_amount_toman"],
            card_number=row["card_number"],
            card_holder=row["card_holder"],
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
            bale_payment_enabled=bool(row["bale_payment_enabled"]),
            bale_provider_token_configured=bool(self.bale_provider_token),
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
            activation_scope=row["activation_scope"],
            platform=row["platform"],
        )
