"""Referral dashboard, admin threshold, and earned-point notices."""

from __future__ import annotations

from html import escape
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..game import GameService
from .shared import safe_edit, telegram_user


class ReferralAdminSetup(StatesGroup):
    waiting_for_required = State()


async def referral_status_text(service: GameService, user_id: int, premium: bool) -> str:
    stats = await service.referrals.stats(user_id)
    progress = (
        "اشتراک ویژه‌ات فعال است. ⭐"
        if premium else
        f"برای فعال‌شدن رایگان اشتراک ویژه، <b>{stats.remaining}</b> "
        "دعوتِ فعال‌شدهٔ دیگر لازم داری."
    )
    return (
        f"👥 دعوت‌ها: <b>{stats.invited}</b> نفر | فعال‌شده‌ها: "
        f"<b>{stats.activated}</b> از <b>{stats.required}</b> نفر. {progress}"
    )


async def deliver_referral_notices(message: Message, service: GameService) -> None:
    """Tell the inviter exactly why each new point was earned on their next entry."""
    user_id = message.from_user.id
    notices, unlocked = await service.referrals.pending_notices(user_id)
    for offset in range(0, len(notices), 20):
        batch = notices[offset : offset + 20]
        lines = [
            (
                f"➕ ۱ امتیاز برای دعوت <b>{escape(notice.display_name)}</b> به ربات."
                if notice.kind == "joined" else
                f"➕ ۱ امتیاز برای فعال‌شدن <b>{escape(notice.display_name)}</b>."
            )
            for notice in batch
        ]
        await message.answer("🎁 <b>امتیازهای دعوت</b>\n\n" + "\n".join(lines))
        await service.referrals.mark_notices(user_id, batch, False)
    if unlocked:
        await message.answer(
            "🎉 <b>تعداد دعوت‌های فعال‌شده‌ات به حد نصاب رسید!</b>\n"
            "اشتراک ویژهٔ بدون انقضای تو رایگان فعال شد. ⭐"
        )
        await service.referrals.mark_notices(user_id, [], True)


def register_handlers(
    router: Router, service: GameService, *, admin_ids: frozenset[int]
) -> None:
    @router.callback_query(F.data == "menu:invite")
    async def invite_menu(callback: CallbackQuery, bot: Bot) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.get_user(callback.from_user.id)
        if user is None:
            return
        token = await service.referrals.code_for(user.telegram_id)
        bot_user = await bot.get_me()
        link = f"https://t.me/{bot_user.username}?start=ref_{token}"
        builder = InlineKeyboardBuilder()
        builder.button(
            text="📤 ارسال دعوت‌نامه",
            url="https://t.me/share/url?url=" + quote(link, safe="")
                + "&text=" + quote("بیا با بازی‌چی بازی کنیم! 🎮", safe=""),
        )
        builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
        builder.adjust(1)
        await safe_edit(
            callback,
            "👥 <b>دعوت دوستان</b>\n\n"
            "برای هر دوستی که با لینک تو وارد ربات شود ۱ امتیاز می‌گیری؛ "
            "پس از فعال‌شدن اشتراکش، ۱ امتیاز دیگر هم می‌گیری. "
            "دعوت از راه لینک بازی یا مسابقهٔ گروهی هم ثبت می‌شود.\n\n"
            + await referral_status_text(service, user.telegram_id, user.is_activated)
            + f"\n\n🔗 لینک اختصاصی تو:\n<code>{link}</code>",
            builder.as_markup(),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:referral_settings")
    async def referral_admin_menu(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user.id not in admin_ids:
            await callback.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        await state.clear()
        required = await service.referrals.required_activations()
        builder = InlineKeyboardBuilder()
        builder.button(text="✏️ تغییر حد نصاب", callback_data="referral_admin:set")
        builder.button(text="↩️ مدیریت ربات", callback_data="menu:admin")
        builder.adjust(1)
        await safe_edit(
            callback,
            f"👥 <b>تنظیمات دعوت</b>\n\nحد نصاب فعال‌سازی: <b>{required}</b> نفر.\n"
            "هر دعوت ۱ امتیاز و هر فعال‌سازی دعوت‌شونده ۱ امتیاز دیگر دارد.",
            builder.as_markup(),
        )
        await callback.answer()

    @router.callback_query(F.data == "referral_admin:set")
    async def referral_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user.id not in admin_ids:
            await callback.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        await state.set_state(ReferralAdminSetup.waiting_for_required)
        await callback.message.answer("حد نصاب جدید را با یک عدد بین ۱ تا ۱۰۰۰ بفرست. برای لغو: /cancel")
        await callback.answer()

    @router.message(ReferralAdminSetup.waiting_for_required, Command("cancel"))
    async def referral_admin_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("تغییر حد نصاب لغو شد.")

    @router.message(ReferralAdminSetup.waiting_for_required, F.text & ~F.text.startswith("/"))
    async def referral_admin_save(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.from_user.id not in admin_ids:
            await state.clear()
            return
        try:
            required = int(message.text.strip())
            unlocked = await service.referrals.set_required_activations(
                message.from_user.id, required
            )
        except ValueError:
            await message.answer("عدد باید بین ۱ تا ۱۰۰۰ باشد؛ دوباره بفرست.")
            return
        await state.clear()
        await message.answer(
            f"✅ حد نصاب دعوت به {required} نفر تغییر کرد. "
            f"اشتراک {len(unlocked)} کاربر واجد شرایط فعال شد."
        )
