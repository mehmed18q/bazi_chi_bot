"""Aiogram handlers for private-chat setup, invitations, and gameplay."""

from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..countdown import (
    CountdownScheduler,
    CountdownService,
    CountdownTargetNotFound,
    CountdownTimeInPast,
    parse_target_datetime,
)
from ..game import GameError, GameService
from ..ui import (
    countdown_users_keyboard,
    menu_keyboard,
)
from .shared import CountdownSetup, QuestionSetup, game_error_text, safe_edit, telegram_user


def register_handlers(
    router: Router,
    service: GameService,
    *,
    countdown_service: CountdownService | None = None,
    countdown_scheduler: CountdownScheduler | None = None,
    countdown_admin_ids: frozenset[int] = frozenset(),
    countdown_timezone: str = "Asia/Tehran",
) -> None:

    def is_countdown_admin(user_id: int) -> bool:
        return user_id in countdown_admin_ids

    async def countdown_picker() -> tuple[str, object]:
        assert countdown_service is not None
        users = await countdown_service.all_users()
        return (
            (
                "💌 <b>تنظیم شمارش‌معکوس</b>\n\n"
                "مخاطب را انتخاب کن. این فهرست فقط شامل کسانی است که قبلاً "
                "ربات را شروع کرده‌اند:"
            ),
            countdown_users_keyboard(users),
        )

    async def require_countdown_admin(message: Message) -> bool:
        if message.from_user is None:
            return False
        if message.from_user.id not in countdown_admin_ids:
            await message.answer("این فرمان فقط برای مدیر ربات فعال است. 🔐")
            return False
        if countdown_service is None:
            await message.answer("سرویس شمارش‌معکوس فعال نیست. ⚠️")
            return False
        await service.save_user(telegram_user(message.from_user))
        return True

    @router.callback_query(F.data.startswith("admin_question:"))
    async def question_kind_selected(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            await callback.answer("این بخش فقط برای ادمین فعال است. 🔐", show_alert=True)
            return
        kind = (callback.data or "").rsplit(":", 1)[-1]
        if kind not in ("truth", "dare"):
            await callback.answer("نوع سؤال معتبر نیست.", show_alert=True)
            return
        await state.set_state(QuestionSetup.waiting_for_text)
        await state.update_data(question_kind=kind)
        await safe_edit(callback, f"✅ نوع انتخاب شد: <b>{'حقیقت' if kind == 'truth' else 'جرئت'}</b>\n\nحالا متن سؤال را بفرست.\n\nبرای لغو: /cancel")
        await callback.answer()

    @router.message(QuestionSetup.waiting_for_text, Command("cancel"))
    async def cancel_question(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("افزودن سؤال لغو شد. ↩️")

    @router.message(QuestionSetup.waiting_for_text, F.text & ~F.text.startswith("/"))
    async def question_text_received(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.from_user.id not in countdown_admin_ids:
            await state.clear()
            return
        data = await state.get_data()
        try:
            question_id = await service.add_question(data["question_kind"], message.text or "")
        except GameError as error:
            await message.answer(game_error_text(error))
            return
        await state.clear()
        await message.answer(f"✅ سؤال به بانک اضافه شد.\nشناسهٔ سؤال: <code>{question_id}</code>")

    @router.message(Command("countdown"))
    async def countdown_command(message: Message, command: CommandObject) -> None:
        if not await require_countdown_admin(message):
            return
        assert message.from_user is not None
        assert countdown_service is not None

        parts = (command.args or "").split()
        if not parts:
            text, keyboard = await countdown_picker()
            await message.answer(text, reply_markup=keyboard)
            return
        if len(parts) != 3:
            await message.answer(
                "فرمت فرمان درست نیست. نمونه:\n"
                "<code>/countdown @username 2026-09-08 21:30</code>\n\n"
                f"🕰 منطقهٔ زمانی: <b>{escape(countdown_timezone)}</b>\n"
                "می‌توانی به‌جای نام کاربری، شناسهٔ عددی تلگرام را بنویسی."
            )
            return

        target = await countdown_service.resolve_user(parts[0])
        if target is None:
            await message.answer(
                "این کاربر را پیدا نکردم. باید قبلاً ربات را استارت کرده باشد و "
                "نام کاربری یا شناسهٔ عددی‌اش درست باشد. 🔍"
            )
            return
        try:
            target_datetime = parse_target_datetime(" ".join(parts[1:]), countdown_timezone)
            _, replaced = await countdown_service.create(
                message.from_user.id,
                target.telegram_id,
                int(target_datetime.timestamp()),
            )
        except ValueError:
            await message.answer(
                "تاریخ معتبر نیست. با این فرمت و بر اساس تقویم میلادی بفرست:\n"
                "<code>YYYY-MM-DD HH:MM</code>"
            )
            return
        except CountdownTimeInPast:
            await message.answer("زمان موعود باید در آینده باشد. ⏰")
            return
        except CountdownTargetNotFound:
            await message.answer("این کاربر دیگر در فهرست ربات پیدا نشد. 🔍")
            return

        if countdown_scheduler is not None:
            countdown_scheduler.wake()
        replacement_text = "\n♻️ شمارش‌معکوس قبلی این کاربر جایگزین شد." if replaced else ""
        await message.answer(
            "✅ <b>شمارش‌معکوس فعال شد!</b>\n\n"
            f"👤 برای: <b>{escape(target.display_name)}</b>\n"
            f"🎯 زمان موعود: <b>{target_datetime:%Y-%m-%d %H:%M}</b>\n"
            f"🕰 منطقهٔ زمانی: <b>{escape(countdown_timezone)}</b>"
            f"{replacement_text}\n\n"
            "اولین پیام تا چند لحظهٔ دیگر ارسال می‌شود. 💌"
        )

    @router.message(Command("cancelcountdown"))
    async def cancel_countdown_command(message: Message, command: CommandObject) -> None:
        if not await require_countdown_admin(message):
            return
        assert countdown_service is not None
        identifier = (command.args or "").strip()
        if not identifier or len(identifier.split()) != 1:
            await message.answer(
                "مخاطب را وارد کن. نمونه:\n<code>/cancelcountdown @username</code>"
            )
            return
        target = await countdown_service.resolve_user(identifier)
        if target is None:
            await message.answer("این کاربر را پیدا نکردم. 🔍")
            return
        cancelled = await countdown_service.cancel_for_target(target.telegram_id)
        if countdown_scheduler is not None:
            countdown_scheduler.wake()
        if cancelled:
            await message.answer(f"🛑 شمارش‌معکوس <b>{escape(target.display_name)}</b> لغو شد.")
        else:
            await message.answer("برای این کاربر شمارش‌معکوس فعالی وجود ندارد.")

    @router.message(Command("countdowns"))
    async def countdowns_command(message: Message) -> None:
        if not await require_countdown_admin(message):
            return
        assert message.from_user is not None
        assert countdown_service is not None
        countdowns = await countdown_service.active_created_by(message.from_user.id)
        if not countdowns:
            await message.answer("فعلاً شمارش‌معکوس فعالی نداری. ⏳")
            return

        lines = ["⏳ <b>شمارش‌معکوس‌های فعال</b>"]
        for countdown in countdowns:
            target = await countdown_service.resolve_user(str(countdown.target_user_id))
            name = target.display_name if target else str(countdown.target_user_id)
            target_time = datetime.fromtimestamp(
                countdown.target_at,
                tz=parse_target_datetime("2000-01-01 00:00", countdown_timezone).tzinfo,
            )
            lines.append(
                f"\n👤 <b>{escape(name)}</b>\n"
                f"🎯 {target_time:%Y-%m-%d %H:%M} — {escape(countdown_timezone)}"
            )
        await message.answer("\n".join(lines))

    @router.message(CountdownSetup.waiting_for_datetime, Command("cancel"))
    async def cancel_countdown_setup(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "تنظیم شمارش‌معکوس لغو شد. ↩️",
            reply_markup=menu_keyboard(is_countdown_admin=True),
        )

    @router.message(CountdownSetup.waiting_for_datetime, F.text & ~F.text.startswith("/"))
    async def countdown_datetime_message(message: Message, state: FSMContext) -> None:
        if not await require_countdown_admin(message):
            await state.clear()
            return
        assert message.from_user is not None
        assert countdown_service is not None
        data = await state.get_data()
        target_id = data.get("countdown_target_user_id")
        target = await countdown_service.resolve_user(str(target_id))
        if target is None:
            await state.clear()
            await message.answer("مخاطب دیگر در دیتابیس پیدا نشد. دوباره تلاش کن. 🔍")
            return
        try:
            target_datetime = parse_target_datetime(message.text, countdown_timezone)
            _, replaced = await countdown_service.create(
                message.from_user.id,
                target.telegram_id,
                int(target_datetime.timestamp()),
            )
        except ValueError:
            await message.answer(
                "تاریخ معتبر نیست؛ دوباره با این قالب بفرست:\n"
                "<code>2026-09-08 15:30</code>\n\n"
                "برای خروج هم <code>/cancel</code> را بفرست."
            )
            return
        except CountdownTimeInPast:
            await message.answer("این زمان گذشته است؛ یک زمان در آینده بفرست. ⏰")
            return
        except CountdownTargetNotFound:
            await state.clear()
            await message.answer("مخاطب دیگر در دیتابیس پیدا نشد. دوباره تلاش کن. 🔍")
            return

        await state.clear()
        if countdown_scheduler is not None:
            countdown_scheduler.wake()
        replacement_text = "\n♻️ زمان‌بندی قبلی این مخاطب جایگزین شد." if replaced else ""
        await message.answer(
            "✅ <b>شمارش‌معکوس با موفقیت فعال شد!</b>\n\n"
            f"👤 مخاطب: <b>{escape(target.display_name)}</b>"
            f"{f' (@{escape(target.username)})' if target.username else ''}\n"
            f"🎯 زمان: <b>{target_datetime:%Y-%m-%d %H:%M}</b>\n"
            f"🕰 منطقهٔ زمانی: <b>{escape(countdown_timezone)}</b>"
            f"{replacement_text}\n\n"
            "اولین پیام تا چند لحظهٔ دیگر برایش ارسال می‌شود. 💌",
            reply_markup=menu_keyboard(is_countdown_admin=True),
        )

    @router.callback_query(F.data == "menu:countdown")
    async def menu_countdown(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not is_countdown_admin(callback.from_user.id):
            await callback.answer("این بخش فقط برای مدیر ربات فعال است. 🔐", show_alert=True)
            return
        if countdown_service is None:
            await callback.answer("سرویس شمارش‌معکوس فعال نیست. ⚠️", show_alert=True)
            return
        text, keyboard = await countdown_picker()
        await safe_edit(callback, text, keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("countdown:page:"))
    async def countdown_users_page(callback: CallbackQuery) -> None:
        if not is_countdown_admin(callback.from_user.id) or countdown_service is None:
            await callback.answer("دسترسی مجاز نیست. 🔐", show_alert=True)
            return
        try:
            page = int((callback.data or "").rsplit(":", 1)[1])
        except ValueError, IndexError:
            await callback.answer("صفحه معتبر نیست.", show_alert=True)
            return
        users = await countdown_service.all_users()
        await safe_edit(
            callback,
            "💌 <b>تنظیم شمارش‌معکوس</b>\n\nمخاطب را انتخاب کن:",
            countdown_users_keyboard(users, page),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("countdown:user:"))
    async def countdown_user_selected(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_countdown_admin(callback.from_user.id) or countdown_service is None:
            await callback.answer("دسترسی مجاز نیست. 🔐", show_alert=True)
            return
        try:
            target_id = int((callback.data or "").rsplit(":", 1)[1])
        except ValueError, IndexError:
            await callback.answer("کاربر معتبر نیست.", show_alert=True)
            return
        target = await countdown_service.resolve_user(str(target_id))
        if target is None:
            await callback.answer("این کاربر دیگر در دیتابیس نیست. 🔍", show_alert=True)
            return
        await state.set_state(CountdownSetup.waiting_for_datetime)
        await state.update_data(countdown_target_user_id=target.telegram_id)
        username = f" (@{escape(target.username)})" if target.username else ""
        await safe_edit(
            callback,
            "✅ مخاطب انتخاب شد:\n"
            f"👤 <b>{escape(target.display_name)}</b>{username}\n\n"
            "حالا تاریخ و ساعت میلادی را به وقت تهران با این قالب بفرست:\n"
            "<code>2026-09-08 15:30</code>\n\n"
            "برای لغو هم <code>/cancel</code> را بفرست.",
        )
        await callback.answer("مخاطب انتخاب شد ✅")
