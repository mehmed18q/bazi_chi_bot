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
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    TelegramObject,
)

from ..game import GameService
from ..models import PaymentSettings, User
from .access import is_free_event
from .keyboards import (
    activation_keyboard,
    activation_scope_keyboard,
    menu_keyboard,
    payment_review_keyboard,
    payment_settings_keyboard,
    profile_name_keyboard,
)
from .shared import ProfileSetup, profile_name_text, safe_edit, safe_send, telegram_user

logger = logging.getLogger(__name__)


class PaymentSetup(StatesGroup):
    waiting_for_receipt = State()


class PaymentAdminSetup(StatesGroup):
    waiting_for_amount = State()
    waiting_for_card = State()
    waiting_for_holder = State()


def _bale_payment_is_active(settings: PaymentSettings) -> bool:
    return settings.bale_payment_enabled and settings.bale_provider_token_configured


def payment_settings_text(settings: PaymentSettings) -> str:
    if _bale_payment_is_active(settings):
        bale_status = "فعال ✅"
    elif not settings.bale_provider_token_configured:
        bale_status = "غیرفعال؛ توکن کیف‌پول در محیط تنظیم نشده 🔒"
    else:
        bale_status = "غیرفعال توسط ادمین 🔒"
    return (
        "💳 <b>تنظیمات خرید اشتراک ویژه</b>\n\n"
        f"مبلغ پایه: <b>{settings.base_amount_toman:,} تومان</b>\n"
        f"شماره کارت: <code>{escape(settings.card_number)}</code>\n"
        f"به نام: <b>{escape(settings.card_holder)}</b>\n\n"
        "مبلغ هر کاربر برابر مبلغ پایه به‌علاوهٔ شناسهٔ داخلی اوست.\n"
        "فعال‌سازی هم‌زمان هر دو پیام‌رسان یک‌ونیم برابر مبلغ یک پلتفرم است؛ "
        "فعال‌سازی پیام‌رسان دوم در زمان بعدی فقط مبلغ پایهٔ همان پلتفرم را دارد.\n"
        "در صورتحساب کیف‌پولی بله همین مبلغ با واحد ریال (ضربدر ۱۰) ارسال می‌شود.\n"
        f"پرداخت کیف‌پولی بله: <b>{bale_status}</b>\n"
        "توکن پرداخت فقط از تنظیمات امن محیط خوانده می‌شود و در پیام‌ها نمایش داده نمی‌شود."
    )


async def activation_gate_text(
    service: GameService, user: User, platform: str = "telegram"
) -> str:
    if user.id is None:
        raise RuntimeError("User has no internal payment id")
    settings = await service.payment_settings()
    try:
        current_amount = await service.expected_payment_amount(
            user, platform=platform, scope="current"
        )
    except ValueError:
        current_amount = None
    try:
        both_amount = await service.expected_payment_amount(
            user, platform=platform, scope="both"
        )
    except ValueError:
        both_amount = None
    pending = await service.latest_pending_payment_receipt(user.telegram_id)
    pending_text = (
        "\n\n⏳ <b>رسیدت ثبت شده و منتظر بررسی ادمین است.</b>" if pending is not None else ""
    )
    native_bale_note = (
        "\n\n🟦 در بله می‌توانی همین اشتراک را با کیف‌پول بله و دکمهٔ پرداخت مستقیم بخری."
        if await service.bale_payment_available()
        else ""
    )
    return (
        "⭐ <b>اشتراک ویژهٔ بازی‌چی (بدون انقضا)</b>\n\n"
        "بازی تک‌نفره، چالش روزانه و یادآور آن رایگان‌اند. با اشتراک ویژه، "
        "بازی دونفره، تورنومنت و مسابقهٔ گروهی و امکانات دیگر باز می‌شوند.\n\n"
        "برای خرید اشتراک ویژه، مبلغ اختصاصی زیر را دقیقاً واریز کن:\n\n"
        + (
            f"💰 فعال‌سازی همین پیام‌رسان: <b>{current_amount:,} تومان</b>\n"
            if current_amount is not None
            else "💰 این پیام‌رسان قبلاً فعال است؛ برای پیام‌رسان دیگر باید مبلغ پایه را بپردازی.\n"
        )
        + (
            f"💰 فعال‌سازی هم‌زمان هر دو پیام‌رسان: <b>{both_amount:,} تومان</b>\n"
            if both_amount is not None
            else ""
        )
        + f"💳 شماره کارت: <code>{escape(settings.card_number)}</code>\n"
        + f"👤 به نام: <b>{escape(settings.card_holder)}</b>\n"
        + f"🆔 شناسهٔ کاربری تو: <code>{user.id}</code>\n\n"
        + "فعال‌سازی هم‌زمان هر دو پیام‌رسان یک‌ونیم برابر هزینهٔ یک پلتفرم است؛ "
        + "انتخاب می‌کنی اشتراک برای همین پیام‌رسان فعال شود یا هر دو پیام‌رسان. "
        + "اگر بعداً پیام‌رسان دوم را فعال کنی، هزینهٔ پایهٔ همان پلتفرم دریافت می‌شود.\n\n"
        + "بعد از واریز، روی «ارسال رسید پرداخت» بزن و رسید را به‌صورت عکس یا متن بفرست. "
        + "مبلغ اختصاصی برای تطبیق پرداخت تو استفاده می‌شود و فعال‌سازی پس از بررسی ادمین انجام خواهد شد."
        + f"{native_bale_note}{pending_text}"
    )


async def show_activation_gate(
    event: Message | CallbackQuery, service: GameService, bot: Bot | None = None
) -> None:
    if event.from_user is None:
        return
    user = await service.get_user(event.from_user.id)
    if user is None:
        await service.save_user(telegram_user(event.from_user))
        user = await service.get_user(event.from_user.id)
    if user is None:
        return
    platform = getattr(bot, "platform", "telegram") if bot is not None else "telegram"
    text = await activation_gate_text(service, user, platform)
    include_bale_payment = bool(
        bot is not None
        and getattr(bot, "platform", "telegram") == "bale"
        and await service.bale_payment_available()
    )
    if isinstance(event, CallbackQuery):
        await safe_edit(event, text, activation_keyboard(include_bale_payment=include_bale_payment))
        await event.answer("این بخش به اشتراک ویژه نیاز دارد.", show_alert=True)
    else:
        await event.answer(
            text,
            reply_markup=activation_keyboard(include_bale_payment=include_bale_payment),
        )


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
        # A successful wallet payment is the event that grants access; it must
        # reach the native-payment handler even while the account is gated.
        if isinstance(event, Message) and event.successful_payment is not None:
            return await handler(event, data)
        if event.from_user.id in self.admin_ids:
            return await handler(event, data)
        if await is_free_event(self.service, event):
            return await handler(event, data)

        state = data.get("state")
        if isinstance(event, Message) and state is not None:
            current_state = await state.get_state()
            command = (event.text or "").split(maxsplit=1)[0].split("@", 1)[0]
            if current_state in {
                PaymentSetup.waiting_for_receipt.state,
                ProfileSetup.waiting_for_name.state,
            } and (not command.startswith("/") or command == "/cancel"):
                return await handler(event, data)

        user = await self.service.get_user(event.from_user.id)
        platform = getattr(data.get("bot"), "platform", "telegram")
        if user is not None and (
            user.is_platform_activated(platform)
            or (data.get("bot") is None and user.is_activated)
        ):
            return await handler(event, data)
        if data.get("bot") is None:
            await show_activation_gate(event, self.service)
        else:
            await show_activation_gate(event, self.service, data.get("bot"))
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
    async def premium_menu(callback: CallbackQuery, bot: Bot) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.get_user(callback.from_user.id)
        if user is None:
            return
        platform = getattr(bot, "platform", "telegram")
        if user.is_platform_activated(platform) or is_admin(user.telegram_id):
            await safe_edit(
                callback,
                "⭐ <b>اشتراک ویژهٔ تو فعال است و تاریخ انقضا ندارد.</b>\n\n"
                "بازی دونفره، تورنومنت، گروه و سایر امکانات در دسترس تو هستند.",
                menu_keyboard(),
            )
            await callback.answer()
            return
        await safe_edit(
            callback,
            await activation_gate_text(service, user, platform),
            activation_keyboard(
                include_bale_payment=(
                    getattr(bot, "platform", "telegram") == "bale"
                    and await service.bale_payment_available()
                )
            ),
        )
        await callback.answer()

    @router.callback_query(F.data == "payment:bale")
    async def bale_payment_start(callback: CallbackQuery, bot: Bot) -> None:
        if getattr(bot, "platform", "telegram") != "bale":
            await callback.answer("پرداخت کیف‌پولی فقط داخل بله انجام می‌شود.", show_alert=True)
            return
        if not await service.bale_payment_available():
            await callback.answer("پرداخت بله فعلاً فعال نیست.", show_alert=True)
            return
        if callback.message is None:
            await callback.answer("پیام پرداخت پیدا نشد.", show_alert=True)
            return
        await callback.message.answer(
            "🟦 <b>دامنهٔ فعال‌سازی پرداخت بله</b>\n\n"
            "انتخاب کن اشتراک فقط برای بله فعال شود یا هم‌زمان برای تلگرام و بله.",
            reply_markup=activation_scope_keyboard(
                method="bale", current_label="🟦 فقط بله"
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("payment:bale:scope:"))
    async def bale_payment_scope(callback: CallbackQuery, bot: Bot) -> None:
        if getattr(bot, "platform", "telegram") != "bale":
            await callback.answer("پرداخت کیف‌پولی فقط داخل بله انجام می‌شود.", show_alert=True)
            return
        scope = (callback.data or "").rsplit(":", 1)[-1]
        if scope not in {"current", "both"} or callback.message is None:
            await callback.answer("انتخاب نامعتبر است.", show_alert=True)
            return
        try:
            payload, amount_irr = await service.create_bale_payment_intent(
                callback.from_user.id, scope=scope
            )
            provider_token = await service.bale_payment_token()
            if provider_token is None:
                raise ValueError("Bale wallet token is missing")
            await callback.message.answer_invoice(
                title="اشتراک ویژه بازی‌چی",
                description=(
                    "اشتراک بدون انقضای بازی‌چی؛ طبق انتخابت برای بله یا هر دو پیام‌رسان"
                ),
                payload=payload,
                provider_token=provider_token,
                currency="IRR",
                prices=[LabeledPrice(label="اشتراک ویژه بازی‌چی", amount=amount_irr)],
            )
        except ValueError as error:
            logger.warning("Could not create Bale payment intent: %s", error)
            await callback.answer("پرداخت بله فعلاً در دسترس نیست.", show_alert=True)
            return
        except TelegramAPIError:
            logger.exception("Bale sendInvoice failed")
            await callback.answer("ارسال صورتحساب بله ناموفق بود؛ دوباره تلاش کن.", show_alert=True)
            return
        await callback.answer("صورتحساب کیف‌پول بله ارسال شد.")

    @router.pre_checkout_query()
    async def bale_pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
        if getattr(bot, "platform", "telegram") != "bale":
            await query.answer(ok=False, error_message="این پرداخت برای بله نیست.")
            return
        try:
            user_id = await service.resolve_platform_user("bale", query.from_user.id)
            valid = await service.validate_bale_pre_checkout(
                user_id=user_id,
                payload=query.invoice_payload,
                amount_irr=query.total_amount,
                currency=query.currency,
            )
        except (ValueError, TypeError):
            valid = False
        await query.answer(
            ok=valid,
            error_message=None if valid else "صورتحساب معتبر نیست؛ دوباره از منوی اشتراک تلاش کن.",
        )

    @router.message(F.successful_payment)
    async def bale_successful_payment(message: Message, bot: Bot) -> None:
        if getattr(bot, "platform", "telegram") != "bale" or message.from_user is None:
            return
        payment = message.successful_payment
        user, changed = await service.complete_bale_payment(
            user_id=message.from_user.id,
            payload=payment.invoice_payload,
            amount_irr=payment.total_amount,
            currency=payment.currency,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
            provider_payment_charge_id=payment.provider_payment_charge_id,
        )
        if user is None:
            await message.answer("پرداخت دریافت شد، اما صورتحساب قابل تطبیق نبود؛ با پشتیبانی تماس بگیر.")
            return
        if not changed:
            await message.answer("این پرداخت قبلاً ثبت شده است. اشتراک حساب تو فعال است. ✅")
            return
        await message.answer(
            "🎉 <b>پرداخت کیف‌پول بله موفق بود؛ اشتراک ویژهٔ بدون انقضا فعال شد.</b>\n\n"
            "دامنهٔ انتخاب‌شده برای فعال‌سازی روی حساب مشترک ثبت شد. "
            "برای فعال‌سازی پیام‌رسان دوم در زمان بعدی، مبلغ پایهٔ همان پلتفرم دریافت می‌شود. 🎮",
            reply_markup=menu_keyboard(),
        )

    @router.callback_query(F.data == "payment:send")
    async def payment_receipt_start(
        callback: CallbackQuery, state: FSMContext, bot: Bot
    ) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.get_user(callback.from_user.id)
        platform = getattr(bot, "platform", "telegram")
        if user is not None and user.is_platform_activated(platform):
            await callback.answer("اشتراک ویژهٔ تو قبلاً فعال شده است. ✅", show_alert=True)
            return
        if callback.message is None:
            await callback.answer("پیام پرداخت پیدا نشد.", show_alert=True)
            return
        await callback.message.answer(
            "📤 <b>دامنهٔ فعال‌سازی رسید</b>\n\n"
            "انتخاب کن رسید برای همین پیام‌رسان باشد یا برای هر دو پیام‌رسان.",
            reply_markup=activation_scope_keyboard(
                method="send", current_label="✈️ فقط همین پیام‌رسان"
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("payment:send:scope:"))
    async def payment_receipt_scope(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        scope = (callback.data or "").rsplit(":", 1)[-1]
        if scope not in {"current", "both"} or callback.message is None:
            await callback.answer("انتخاب نامعتبر است.", show_alert=True)
            return
        await state.update_data(activation_scope=scope)
        await state.set_state(PaymentSetup.waiting_for_receipt)
        user = await service.get_user(callback.from_user.id)
        amount_note = ""
        if user is not None:
            try:
                amount = await service.expected_payment_amount(
                    user, platform=getattr(bot, "platform", "telegram"), scope=scope
                )
                amount_note = f"\nمبلغ مورد انتظار: <b>{amount:,} تومان</b>"
            except ValueError:
                pass
        await callback.message.answer(
            "📤 <b>ارسال رسید</b>\n\n"
            "حالا عکس رسید یا متن شامل مشخصات واریز را بفرست."
            f"{amount_note}\n"
            "برای لغو: /cancel"
        )
        await callback.answer()

    @router.message(PaymentSetup.waiting_for_receipt, Command("cancel"))
    async def payment_receipt_cancel(message: Message, state: FSMContext, bot: Bot) -> None:
        await state.clear()
        await show_activation_gate(message, service, bot)

    @router.message(
        PaymentSetup.waiting_for_receipt,
        F.photo | (F.text & ~F.text.startswith("/")),
    )
    async def payment_receipt_submit(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        await service.save_user(telegram_user(message.from_user))
        photo = message.photo[-1].file_id if message.photo else None
        state_data = await state.get_data() if hasattr(state, "get_data") else {}
        scope = state_data.get("activation_scope", "current")
        platform = getattr(bot, "platform", "telegram")
        try:
            receipt = await service.submit_payment_receipt(
                message.from_user.id,
                receipt_text=message.text or message.caption,
                telegram_file_id=photo,
                platform=platform,
                scope=scope,
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
        scope_label = (
            "هر دو پیام‌رسان"
            if receipt.activation_scope == "both"
            else "بله" if receipt.activation_scope == "bale" else "تلگرام"
        )
        admin_text = (
            f"💳 <b>رسید اشتراک ویژه #{receipt.id}</b>\n\n"
            f"نام در ربات: <b>{escape(user.display_name)}</b>\n"
            f"نام تلگرام: <b>{escape(telegram_name)}</b>\n"
            f"یوزرنیم: <code>{escape(username)}</code>\n"
            f"شناسهٔ داخلی: <code>{receipt.user_internal_id}</code>\n"
            f"شناسهٔ تلگرام: <code>{user.telegram_id}</code>\n"
            f"دامنهٔ فعال‌سازی: <b>{scope_label}</b>\n"
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

    @router.message(PaymentSetup.waiting_for_receipt, ~F.text.startswith("/"))
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
                (
                    "🎉 <b>پرداختت تأیید شد؛ اشتراک ویژهٔ بدون انقضا فعال شد.</b>\n\n"
                    + (
                        "فعال‌سازی برای تلگرام و بله انجام شد.\n"
                        if receipt.activation_scope == "both"
                        else f"فعال‌سازی برای {('بله' if receipt.activation_scope == 'bale' else 'تلگرام')} انجام شد.\n"
                    )
                    + "حالا می‌تونی همهٔ بازی‌ها را تجربه کنی! 🎮"
                )
                + f"{pending_invite_note}\n\n{profile_name_text(user)}",
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
        settings = await service.payment_settings()
        await safe_edit(
            callback,
            payment_settings_text(settings),
            payment_settings_keyboard(bale_payment_enabled=await service.bale_payment_available()),
        )
        await callback.answer()

    @router.callback_query(F.data == "admin_payment:bale:toggle")
    async def bale_payment_toggle(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        current = await service.payment_settings()
        try:
            updated = await service.update_payment_settings(
                callback.from_user.id,
                bale_payment_enabled=not current.bale_payment_enabled,
            )
        except ValueError:
            await callback.answer(
                "برای فعال‌سازی، ابتدا BALE_PAYMENT_PROVIDER_TOKEN را در محیط تنظیم کن.",
                show_alert=True,
            )
            return
        await safe_edit(
            callback,
            payment_settings_text(updated),
            payment_settings_keyboard(
                bale_payment_enabled=await service.bale_payment_available()
            ),
        )
        await callback.answer("وضعیت پرداخت بله تغییر کرد.")

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
        settings = await service.payment_settings()
        await message.answer(
            payment_settings_text(settings),
            reply_markup=payment_settings_keyboard(
                bale_payment_enabled=await service.bale_payment_available()
            ),
        )

    @router.message(StateFilter(PaymentAdminSetup), F.text & ~F.text.startswith("/"))
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
            reply_markup=payment_settings_keyboard(
                bale_payment_enabled=await service.bale_payment_available()
            ),
        )
