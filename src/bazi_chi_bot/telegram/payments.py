"""Activation payment gate, receipt collection, and admin controls."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..game import GameService
from ..models import PaymentSettings, User
from .access import is_free_event
from .keyboards import (
    activation_keyboard,
    menu_keyboard,
    payment_review_keyboard,
    payment_settings_keyboard,
    profile_name_keyboard,
)
from .shared import profile_name_text, safe_edit, safe_send, telegram_user

logger = logging.getLogger(__name__)


class PaymentSetup(StatesGroup):
    waiting_for_receipt = State()


class PaymentAdminSetup(StatesGroup):
    waiting_for_amount = State()
    waiting_for_card = State()
    waiting_for_holder = State()


def payment_settings_text(settings: PaymentSettings) -> str:
    return (
        "💳 <b>تنظیمات خرید اشتراک ویژه</b>\n\n"
        f"مبلغ پایه: <b>{settings.base_amount_toman:,} تومان</b>\n"
        f"شماره کارت: <code>{escape(settings.card_number)}</code>\n"
        f"به نام: <b>{escape(settings.card_holder)}</b>\n\n"
        "مبلغ هر کاربر برابر مبلغ پایه به‌علاوهٔ شناسهٔ داخلی اوست."
    )


async def activation_gate_text(service: GameService, user: User) -> str:
    if user.id is None:
        raise RuntimeError("User has no internal payment id")
    settings = await service.payment_settings()
    amount = settings.base_amount_toman + user.id
    pending = await service.latest_pending_payment_receipt(user.telegram_id)
    pending_text = (
        "\n\n⏳ <b>رسیدت ثبت شده و منتظر بررسی ادمین است.</b>" if pending is not None else ""
    )
    return (
        "⭐ <b>اشتراک ویژهٔ بازی‌چی (بدون انقضا)</b>\n\n"
        "بازی تک‌نفره، چالش روزانه و یادآور آن رایگان‌اند. با اشتراک ویژه، "
        "بازی دونفره، تورنومنت و مسابقهٔ گروهی و امکانات دیگر باز می‌شوند.\n\n"
        "برای خرید اشتراک ویژه، مبلغ اختصاصی زیر را دقیقاً واریز کن:\n\n"
        f"💰 مبلغ: <b>{amount:,} تومان</b>\n"
        f"💳 شماره کارت: <code>{escape(settings.card_number)}</code>\n"
        f"👤 به نام: <b>{escape(settings.card_holder)}</b>\n"
        f"🆔 شناسهٔ کاربری تو: <code>{user.id}</code>\n\n"
        "بعد از واریز، روی «ارسال رسید پرداخت» بزن و رسید را به‌صورت عکس یا متن بفرست. "
        "مبلغ اختصاصی برای تطبیق پرداخت تو استفاده می‌شود و فعال‌سازی پس از بررسی ادمین انجام خواهد شد."
        f"{pending_text}"
    )


async def show_activation_gate(event: Message | CallbackQuery, service: GameService) -> None:
    if event.from_user is None:
        return
    user = await service.get_user(event.from_user.id)
    if user is None:
        await service.save_user(telegram_user(event.from_user))
        user = await service.get_user(event.from_user.id)
    if user is None:
        return
    text = await activation_gate_text(service, user)
    if isinstance(event, CallbackQuery):
        await safe_edit(event, text, activation_keyboard())
        await event.answer("این بخش به اشتراک ویژه نیاز دارد.", show_alert=True)
    else:
        await event.answer(text, reply_markup=activation_keyboard())


class PaymentAccessMiddleware(BaseMiddleware):
    """Permit free play and require an approved purchase for premium routes."""

    def __init__(self, service: GameService, admin_ids: frozenset[int]) -> None:
        self.service = service
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)) or event.from_user is None:
            return await handler(event, data)
        if event.from_user.id in self.admin_ids:
            return await handler(event, data)
        if await is_free_event(self.service, event):
            return await handler(event, data)

        state = data.get("state")
        if state is not None and await state.get_state() == PaymentSetup.waiting_for_receipt.state:
            return await handler(event, data)

        user = await self.service.get_user(event.from_user.id)
        if user is not None and user.is_activated:
            return await handler(event, data)
        await show_activation_gate(event, self.service)
        return None


def register_handlers(
    router: Router,
    service: GameService,
    *,
    admin_ids: frozenset[int],
    reviewer_ids: frozenset[int],
) -> None:
    def is_admin(user_id: int) -> bool:
        return user_id in admin_ids

    @router.callback_query(F.data == "menu:premium")
    async def premium_menu(callback: CallbackQuery) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.get_user(callback.from_user.id)
        if user is None:
            return
        if user.is_activated or is_admin(user.telegram_id):
            await safe_edit(
                callback,
                "⭐ <b>اشتراک ویژهٔ تو فعال است و تاریخ انقضا ندارد.</b>\n\n"
                "بازی دونفره، تورنومنت، گروه و سایر امکانات در دسترس تو هستند.",
                menu_keyboard(),
            )
            await callback.answer()
            return
        await safe_edit(callback, await activation_gate_text(service, user), activation_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "payment:send")
    async def payment_receipt_start(callback: CallbackQuery, state: FSMContext) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.get_user(callback.from_user.id)
        if user is not None and user.is_activated:
            await callback.answer("اشتراک ویژهٔ تو قبلاً فعال شده است. ✅", show_alert=True)
            return
        await state.set_state(PaymentSetup.waiting_for_receipt)
        await callback.message.answer(
            "📤 <b>ارسال رسید</b>\n\n"
            "حالا عکس رسید یا متن شامل مشخصات واریز را بفرست.\n"
            "برای لغو: /cancel"
        )
        await callback.answer()

    @router.message(PaymentSetup.waiting_for_receipt, Command("cancel"))
    async def payment_receipt_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_activation_gate(message, service)

    @router.message(PaymentSetup.waiting_for_receipt, F.text | F.photo)
    async def payment_receipt_submit(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        await service.save_user(telegram_user(message.from_user))
        photo = message.photo[-1].file_id if message.photo else None
        try:
            receipt = await service.submit_payment_receipt(
                message.from_user.id,
                receipt_text=message.text or message.caption,
                telegram_file_id=photo,
            )
        except ValueError as error:
            if "already activated" in str(error):
                await state.clear()
                await message.answer(
                    "✅ اشتراک ویژهٔ تو قبلاً فعال شده است.",
                    reply_markup=menu_keyboard(),
                )
            else:
                await message.answer("رسید معتبر نیست؛ عکس یا متن کوتاه‌تری بفرست.")
            return

        user = await service.get_user(message.from_user.id)
        if user is None:
            return
        username = f"@{user.username}" if user.username else "ندارد"
        telegram_name = user.telegram_display_name or user.display_name
        receipt_note = (
            f"\n\n📝 متن رسید:\n{escape(receipt.receipt_text)}" if receipt.receipt_text else ""
        )
        admin_text = (
            f"💳 <b>رسید اشتراک ویژه #{receipt.id}</b>\n\n"
            f"نام در ربات: <b>{escape(user.display_name)}</b>\n"
            f"نام تلگرام: <b>{escape(telegram_name)}</b>\n"
            f"یوزرنیم: <code>{escape(username)}</code>\n"
            f"شناسهٔ داخلی: <code>{receipt.user_internal_id}</code>\n"
            f"شناسهٔ تلگرام: <code>{user.telegram_id}</code>\n"
            f"مبلغ مورد انتظار: <b>{receipt.expected_amount_toman:,} تومان</b>"
            f"{receipt_note}"
        )
        for reviewer_id in reviewer_ids:
            try:
                await bot.forward_message(
                    chat_id=reviewer_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
            except TelegramAPIError as error:
                logger.warning("Could not forward payment receipt %s: %s", receipt.id, error)
            await safe_send(
                bot,
                reviewer_id,
                admin_text,
                payment_review_keyboard(receipt.id),
            )

        await state.clear()
        await message.answer(
            "✅ <b>رسیدت ثبت شد.</b>\n\nبعد از بررسی ادمین، نتیجه همینجا برایت ارسال می‌شود.",
            reply_markup=activation_keyboard(),
        )

    @router.message(PaymentSetup.waiting_for_receipt)
    async def payment_receipt_invalid(message: Message) -> None:
        await message.answer("فقط عکس رسید یا متن مشخصات واریز را بفرست.")

    @router.callback_query(
        F.data.startswith("payment:approve:") | F.data.startswith("payment:reject:")
    )
    async def payment_review(callback: CallbackQuery, bot: Bot) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        try:
            receipt_id = int(parts[2])
        except IndexError, ValueError:
            await callback.answer("رسید معتبر نیست.", show_alert=True)
            return
        approved = parts[1] == "approve"
        receipt, changed = await service.review_payment_receipt(
            receipt_id, callback.from_user.id, approved
        )
        if receipt is None:
            await callback.answer("رسید پیدا نشد.", show_alert=True)
            return
        if not changed:
            await callback.answer("این رسید قبلاً بررسی شده است.", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
        if approved:
            user = await service.get_user(receipt.user_telegram_id)
            if user is None:
                await callback.answer("کاربر این رسید پیدا نشد.", show_alert=True)
                return
            pending_invite_note = (
                "\n\nدعوت قبلی‌ات ذخیره شده؛ برای ورود به همان بازی /start را بزن."
                if user.pending_invite_token
                else ""
            )
            await safe_send(
                bot,
                receipt.user_telegram_id,
                "🎉 <b>پرداختت تأیید شد؛ اشتراک ویژهٔ بدون انقضا فعال شد.</b>\n\nحالا می‌تونی همهٔ بازی‌ها را تجربه کنی! 🎮"
                f"{pending_invite_note}\n\n{profile_name_text(user)}",
                profile_name_keyboard(user.nickname_is_custom),
            )
            await callback.answer("کاربر فعال شد. ✅")
        else:
            await safe_send(
                bot,
                receipt.user_telegram_id,
                "❌ <b>رسید پرداخت تأیید نشد.</b>\n\n"
                "اطلاعات واریز را بررسی کن و یک رسید معتبر دوباره بفرست.",
                activation_keyboard(),
            )
            await callback.answer("رسید رد شد.")

    @router.callback_query(F.data == "menu:payments")
    async def payment_admin_menu(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        await state.clear()
        await safe_edit(
            callback,
            payment_settings_text(await service.payment_settings()),
            payment_settings_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("admin_payment:set:"))
    async def payment_setting_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        field = (callback.data or "").rsplit(":", 1)[-1]
        states = {
            "amount": PaymentAdminSetup.waiting_for_amount,
            "card": PaymentAdminSetup.waiting_for_card,
            "holder": PaymentAdminSetup.waiting_for_holder,
        }
        prompts = {
            "amount": "مبلغ پایهٔ جدید را به تومان و فقط با رقم بفرست.",
            "card": "شماره کارت جدید را بفرست.",
            "holder": "نام صاحب کارت را بفرست.",
        }
        if field not in states:
            await callback.answer("گزینه معتبر نیست.", show_alert=True)
            return
        await state.set_state(states[field])
        await callback.message.answer(f"{prompts[field]}\nبرای لغو: /cancel")
        await callback.answer()

    @router.message(StateFilter(PaymentAdminSetup), Command("cancel"))
    async def payment_setting_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            payment_settings_text(await service.payment_settings()),
            reply_markup=payment_settings_keyboard(),
        )

    @router.message(StateFilter(PaymentAdminSetup), F.text)
    async def payment_setting_save(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        current_state = await state.get_state()
        kwargs: dict[str, object]
        if current_state == PaymentAdminSetup.waiting_for_amount.state:
            normalized = message.text.replace(",", "").replace("٬", "").strip()
            if not normalized.isdigit():
                await message.answer("مبلغ را فقط با رقم و به تومان بفرست.")
                return
            kwargs = {"base_amount_toman": int(normalized)}
        elif current_state == PaymentAdminSetup.waiting_for_card.state:
            kwargs = {"card_number": message.text}
        elif current_state == PaymentAdminSetup.waiting_for_holder.state:
            kwargs = {"card_holder": message.text}
        else:
            return
        try:
            settings = await service.update_payment_settings(message.from_user.id, **kwargs)
        except ValueError:
            await message.answer("مقدار واردشده معتبر نیست؛ دوباره بفرست.")
            return
        await state.clear()
        await message.answer(
            "✅ تنظیمات پرداخت ذخیره شد.\n\n" + payment_settings_text(settings),
            reply_markup=payment_settings_keyboard(),
        )
