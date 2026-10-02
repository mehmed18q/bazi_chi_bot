"""Aiogram handlers for private-chat setup, invitations, and gameplay."""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..game import (
    GameError,
    GameService,
)
from ..models import GameType
from ..ui import (
    HELP_TEXT,
    START_TEXT,
    SUPPORT_TEXT,
    leaderboard_text,
    menu_keyboard,
    question_kind_keyboard,
    stats_text,
)
from .keyboards import (
    activation_keyboard,
    admin_menu_keyboard,
    admin_user_actions,
    admin_users_keyboard,
    profile_name_keyboard,
    sponsor_admin_keyboard,
    sponsor_kind_keyboard,
)
from .payments import activation_gate_text
from .shared import (
    AdminMessageSetup,
    GamePresenter,
    ProfileSetup,
    QuestionSetup,
    SponsorSetup,
    game_error_text,
    profile_name_text,
    refresh_profile_photo,
    safe_edit,
    safe_send,
    telegram_user,
    view_for,
)
from .sponsors import channel_admin_status, check_sponsors, show_sponsor_gate


def register_handlers(
    router: Router,
    service: GameService,
    *,
    countdown_admin_ids: frozenset[int] = frozenset(),
) -> None:
    presenter = GamePresenter(service)
    send_game_view = presenter.send_game_view

    def is_countdown_admin(user_id: int) -> bool:
        return user_id in countdown_admin_ids

    def main_menu(user_id: int, has_active_games: bool = False):
        return menu_keyboard(has_active_games, is_countdown_admin(user_id))

    def admin_profile_text(user, stats) -> str:
        username = f"@{user.username}" if user.username else "ندارد"
        telegram_name = user.telegram_display_name or user.display_name
        return (
            "👤 <b>پروندهٔ کاربر</b>\n\n"
            f"نام در ربات: <b>{escape(user.display_name)}</b>\n"
            f"نام تلگرام: <b>{escape(telegram_name)}</b>\n"
            f"یوزرنیم: <code>{escape(username)}</code>\n"
            f"شناسهٔ داخلی: <code>{user.id if user.id is not None else '—'}</code>\n"
            f"شناسهٔ تلگرام: <code>{user.telegram_id}</code>\n"
            f"وضعیت فعال‌سازی: <b>{'فعال ✅' if user.is_activated else 'غیرفعال 🔒'}</b>\n\n"
            f"بازی‌ها: {stats.games_played}\nبرد: {stats.wins}\nامتیاز: {stats.points_won}"
        )

    def game_title(game_type: GameType) -> str:
        return {
            GameType.GOL_YA_POOCH: "گل یا پوچ 🌸",
            GameType.TIC_TAC_TOE: "دوز سه‌تایی ❌⭕",
            GameType.ROCK_PAPER_SCISSORS: "سنگ، کاغذ، قیچی ✊✋✌️",
            GameType.WORD_GUESS: "حدس کلمه 🔤",
            GameType.MASTERMIND: "فکر بکر 🎨",
            GameType.TRUTH_OR_DARE: "جرئت یا حقیقت 🎭",
        }[game_type]

    @router.callback_query(F.data == "menu:add_question")
    async def add_question_menu(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            await callback.answer("این بخش فقط برای ادمین فعال است. 🔐", show_alert=True)
            return
        await state.set_state(QuestionSetup.waiting_for_kind)
        await safe_edit(
            callback, "📝 <b>افزودن سؤال</b>\n\nنوع سؤال را انتخاب کن:", question_kind_keyboard()
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:admin")
    async def admin_menu(callback: CallbackQuery) -> None:
        if not is_countdown_admin(callback.from_user.id):
            await callback.answer("این بخش فقط برای ادمین فعال است. 🔐", show_alert=True)
            return
        await safe_edit(
            callback,
            "⚙️ <b>مدیریت ربات</b>\n\nیکی از بخش‌های مدیریتی را انتخاب کن:",
            admin_menu_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:sponsors")
    async def sponsor_menu(callback: CallbackQuery) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            await callback.answer("این بخش فقط برای ادمین فعال است. 🔐", show_alert=True)
            return
        await safe_edit(
            callback,
            "📣 <b>مدیریت اسپانسرها</b>\n\nاسپانسرهای فعال با ✅ نمایش داده می‌شوند.",
            sponsor_admin_keyboard(await service.all_sponsors()),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:users")
    async def admin_users(callback: CallbackQuery) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            await callback.answer("این بخش فقط برای ادمین فعال است. 🔐", show_alert=True)
            return
        await safe_edit(
            callback,
            "👥 <b>کاربران ربات</b>\n\nیک کاربر را برای مشاهدهٔ پرونده انتخاب کن:",
            admin_users_keyboard(await service.all_users()),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("admin_user:view:"))
    async def admin_user_profile(callback: CallbackQuery, bot: Bot) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            return
        uid = int(callback.data.rsplit(":", 1)[-1])
        user = await service.get_user(uid)
        if user is None:
            await callback.answer("کاربر پیدا نشد.", show_alert=True)
            return
        stats = await service.get_stats(uid)
        text = admin_profile_text(user, stats)
        photo_file_id = user.profile_photo_file_id
        if photo_file_id is None:
            photo_file_id = await refresh_profile_photo(service, bot, uid)
        if photo_file_id is not None:
            try:
                await callback.message.delete()
                await callback.message.answer_photo(
                    photo_file_id,
                    caption=text,
                    reply_markup=admin_user_actions(uid, user.is_activated),
                )
            except TelegramAPIError:
                await service.set_user_profile_photo(uid, None)
                await callback.message.answer(
                    text, reply_markup=admin_user_actions(uid, user.is_activated)
                )
            await callback.answer()
            return
        await safe_edit(callback, text, admin_user_actions(uid, user.is_activated))
        await callback.answer()

    @router.callback_query(F.data.startswith("admin_user:activation:"))
    async def admin_user_activation(callback: CallbackQuery, bot: Bot) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            await callback.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        try:
            uid = int(parts[2])
            active = {"on": True, "off": False}[parts[3]]
        except IndexError, KeyError, ValueError:
            await callback.answer("درخواست معتبر نیست.", show_alert=True)
            return
        user = await service.set_user_activation(uid, callback.from_user.id, active)
        if user is None:
            await callback.answer("کاربر پیدا نشد.", show_alert=True)
            return
        stats = await service.get_stats(uid)
        text = admin_profile_text(user, stats)
        keyboard = admin_user_actions(uid, user.is_activated)
        if callback.message is not None and getattr(callback.message, "photo", None):
            await callback.message.edit_caption(caption=text, reply_markup=keyboard)
        else:
            await safe_edit(callback, text, keyboard)
        if active:
            pending_invite_note = (
                "\n\nدعوت قبلی‌ات ذخیره شده؛ برای ورود به همان بازی /start را بزن."
                if user.pending_invite_token
                else ""
            )
            await safe_send(
                bot,
                uid,
                "✅ <b>حساب تو توسط ادمین فعال شد.</b>\n\nحالا می‌تونی بازی کنی! 🎮"
                f"{pending_invite_note}\n\n{profile_name_text(user)}",
                profile_name_keyboard(user.nickname_is_custom),
            )
            await callback.answer("کاربر فعال شد. ✅")
        else:
            await safe_send(
                bot,
                uid,
                "🔒 <b>دسترسی تو به ربات توسط ادمین غیرفعال شد.</b>\n\n"
                "برای فعال‌سازی دوباره، /start را بزن.",
                activation_keyboard(),
            )
            await callback.answer("کاربر غیرفعال شد.")

    @router.callback_query(F.data.startswith("admin_user:message:"))
    async def admin_message_start(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            return
        uid = int(callback.data.rsplit(":", 1)[-1])
        await state.set_state(AdminMessageSetup.waiting_for_text)
        await state.update_data(admin_message_target=uid)
        await callback.message.answer("متن پیام را بفرست. برای لغو /cancel")
        await callback.answer()

    @router.message(AdminMessageSetup.waiting_for_text, Command("cancel"))
    async def admin_message_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("ارسال پیام لغو شد.")

    @router.message(AdminMessageSetup.waiting_for_text, F.text)
    async def admin_message_send(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.from_user.id not in countdown_admin_ids:
            await state.clear()
            return
        target = (await state.get_data()).get("admin_message_target")
        try:
            await bot.send_message(target, message.text)
        except TelegramAPIError:
            await message.answer("ارسال پیام ناموفق بود؛ شاید کاربر ربات را بلاک کرده باشد.")
        else:
            await message.answer("✅ پیام ارسال شد.")
        await state.clear()

    @router.callback_query(F.data == "admin_sponsor:add")
    async def sponsor_add(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            await callback.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        await state.set_state(SponsorSetup.waiting_for_details)
        await state.update_data(sponsor_kind=None)
        await safe_edit(callback, "نوع اسپانسر را انتخاب کن:", sponsor_kind_keyboard())
        await callback.answer()

    @router.callback_query(F.data.startswith("admin_sponsor:kind:"))
    async def sponsor_kind(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            return
        kind = callback.data.rsplit(":", 1)[-1]
        await state.update_data(sponsor_kind=kind)
        example = (
            "عنوان | @channel_username یا -100... | https://t.me/channel"
            if kind == "channel"
            else "عنوان | @bot_username | https://t.me/bot_username"
        )
        await callback.message.answer(
            f"اطلاعات را در یک خط بفرست:\n<code>{example}</code>\nبرای لغو /cancel"
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("admin_sponsor:toggle:"))
    async def sponsor_toggle(callback: CallbackQuery, bot: Bot) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            return
        sid = int(callback.data.rsplit(":", 1)[-1])
        sponsor = next((s for s in await service.all_sponsors() if s.id == sid), None)
        if sponsor:
            if not sponsor.active and sponsor.kind == "channel":
                admin_status = await channel_admin_status(bot, sponsor)
                if admin_status is not True:
                    message = (
                        "بازی‌چی هنوز ادمین این کانال نیست. اول ربات را ادمین کن."
                        if admin_status is False
                        else "فعلاً امکان بررسی کانال نیست؛ کمی بعد دوباره تلاش کن."
                    )
                    await callback.answer(message, show_alert=True)
                    return
            await service.set_sponsor_active(sid, not sponsor.active)
        await safe_edit(
            callback,
            "📣 <b>مدیریت اسپانسرها</b>",
            sponsor_admin_keyboard(await service.all_sponsors()),
        )
        await callback.answer("وضعیت به‌روزرسانی شد.")

    @router.callback_query(F.data.startswith("admin_sponsor:delete:"))
    async def sponsor_delete(callback: CallbackQuery) -> None:
        if callback.from_user.id not in countdown_admin_ids:
            return
        await service.delete_sponsor(int(callback.data.rsplit(":", 1)[-1]))
        await safe_edit(
            callback,
            "📣 <b>مدیریت اسپانسرها</b>",
            sponsor_admin_keyboard(await service.all_sponsors()),
        )
        await callback.answer("حذف شد.")

    @router.message(SponsorSetup.waiting_for_details, Command("cancel"))
    async def sponsor_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "افزودن اسپانسر لغو شد.", reply_markup=menu_keyboard(is_countdown_admin=True)
        )

    @router.message(SponsorSetup.waiting_for_details, F.text)
    async def sponsor_details(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        kind = data.get("sponsor_kind")
        if not kind:
            await message.answer("اول نوع اسپانسر را از دکمه‌ها انتخاب کن.")
            return
        parts = [part.strip() for part in message.text.split("|")]
        if len(parts) != 3 or not all(parts):
            await message.answer("فرمت درست: عنوان | شناسه یا یوزرنیم | لینک")
            return
        title, identifier, url = parts
        try:
            if kind == "channel":
                chat = await bot.get_chat(identifier)
                if chat.type not in {ChatType.CHANNEL, ChatType.SUPERGROUP}:
                    await message.answer("این شناسه متعلق به کانال یا سوپرگروه نیست.")
                    return
                me = await bot.get_me()
                bot_member = await bot.get_chat_member(chat.id, me.id)
                if bot_member.status not in {
                    ChatMemberStatus.CREATOR,
                    ChatMemberStatus.ADMINISTRATOR,
                }:
                    await message.answer(
                        "اول بازی‌چی را در کانال ادمین کن، بعد دوباره اطلاعات را بفرست."
                    )
                    return
                await service.add_sponsor(kind, title, url, chat_id=str(chat.id))
            else:
                await service.add_sponsor(kind, title, url, username=identifier)
        except TelegramAPIError:
            await message.answer(
                "به کانال دسترسی ندارم. یوزرنیم را بررسی کن و بازی‌چی را در کانال "
                "به‌عنوان ادمین اضافه کن."
            )
            return
        except Exception:
            await message.answer("این اسپانسر قبلاً ثبت شده یا اطلاعاتش معتبر نیست.")
            return
        await state.clear()
        await message.answer(
            "✅ اسپانسر اضافه شد.",
            reply_markup=sponsor_admin_keyboard(await service.all_sponsors()),
        )

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject, bot: Bot, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.clear()
        user = telegram_user(message.from_user)
        admin = is_countdown_admin(user.telegram_id)
        await service.register_user_entry(user, activation_exempt=admin)
        await refresh_profile_photo(service, bot, user.telegram_id)
        payload = command.args or ""
        persisted_user = await service.get_user(user.telegram_id)
        invite_token = payload.removeprefix("join_") if payload.startswith("join_") else ""
        if invite_token and persisted_user is not None:
            await service.set_pending_invite(user.telegram_id, invite_token)
            persisted_user = await service.get_user(user.telegram_id)
        if not admin and persisted_user is not None and not persisted_user.is_activated:
            await message.answer(START_TEXT)
            await message.answer(
                await activation_gate_text(service, persisted_user),
                reply_markup=activation_keyboard(),
            )
            return
        if not payload and persisted_user is not None and persisted_user.pending_invite_token:
            payload = f"join_{persisted_user.pending_invite_token}"
        if not admin:
            sponsors = await service.active_sponsors()
            check = await check_sponsors(bot, user.telegram_id, sponsors)
            if not check.allowed:
                await show_sponsor_gate(message, check, payload=payload)
                return
        if payload.startswith("join_"):
            token = payload.removeprefix("join_")
            try:
                game = await service.join_game(token, user.telegram_id)
            except GameError as error:
                await service.set_pending_invite(user.telegram_id, None)
                await message.answer(game_error_text(error))
            else:
                await service.set_pending_invite(user.telegram_id, None)
                await message.answer(
                    "🤝 <b>به بازی پیوستی!</b>\n\n"
                    f"بازی انتخابی: <b>{game_title(game.game_type)}</b>\n"
                    "آماده‌ای؟ بزن بریم! 🏆"
                )
                await send_game_view(bot, game, user.telegram_id)
                await send_game_view(
                    bot,
                    game,
                    game.creator_id,
                    f"🎉 <b>{escape(persisted_user.display_name)}</b> به بازی پیوست.\n"
                    "بازی شروع شد!",
                    fresh=True,
                )
                return

        games = await service.active_games(user.telegram_id)
        recovery = (
            f"\n\n💾 <b>{len(games)} بازی ناتمام</b> داری؛ از «ادامهٔ بازی» وارد شو."
            if games
            else ""
        )
        await message.answer(
            START_TEXT + recovery,
            reply_markup=main_menu(user.telegram_id, bool(games)),
        )

    @router.callback_query(F.data.startswith("sponsors:check"))
    async def sponsors_check(callback: CallbackQuery, bot: Bot) -> None:
        if is_countdown_admin(callback.from_user.id):
            await safe_edit(
                callback,
                "✅ مدیر ربات به عضویت در اسپانسرها نیاز ندارد.",
                main_menu(callback.from_user.id),
            )
            await callback.answer("دسترسی مدیر فعال است. ✅")
            return
        sponsors = await service.active_sponsors()
        check = await check_sponsors(bot, callback.from_user.id, sponsors)
        if not check.allowed:
            await show_sponsor_gate(callback, check)
            return

        payload = (callback.data or "").removeprefix("sponsors:check:")
        if payload.startswith("join_"):
            user = telegram_user(callback.from_user)
            await service.save_user(user)
            persisted_joiner = await service.get_user(user.telegram_id)
            token = payload.removeprefix("join_")
            try:
                game = await service.join_game(token, user.telegram_id)
            except GameError as error:
                await service.set_pending_invite(user.telegram_id, None)
                await safe_edit(callback, game_error_text(error), menu_keyboard())
            else:
                await service.set_pending_invite(user.telegram_id, None)
                await safe_edit(
                    callback,
                    "✅ <b>عضویت تأیید شد و به بازی پیوستی!</b>",
                )
                await send_game_view(bot, game, user.telegram_id)
                await send_game_view(
                    bot,
                    game,
                    game.creator_id,
                    f"🎉 <b>{escape(persisted_joiner.display_name if persisted_joiner else user.display_name)}</b> "
                    "به بازی پیوست.\nبازی شروع شد!",
                    fresh=True,
                )
            await callback.answer("عضویت تأیید شد! 🎉")
            return

        games = await service.active_games(callback.from_user.id)
        await safe_edit(
            callback,
            "✅ <b>عضویتت تأیید شد!</b>\n\nحالا می‌تونی بازی رو شروع کنی.",
            menu_keyboard(bool(games)),
        )
        await callback.answer("تأیید شد! 🎉")

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(HELP_TEXT, reply_markup=main_menu(message.from_user.id))

    @router.message(Command("users"))
    async def users_command(message: Message) -> None:
        if message.from_user is None or message.from_user.id not in countdown_admin_ids:
            await message.answer("این فرمان فقط برای مدیر ربات فعال است. 🔐")
            return
        await message.answer(
            "👥 <b>کاربران ربات</b>\n\nیک کاربر را انتخاب کن:",
            reply_markup=admin_users_keyboard(await service.all_users()),
        )

    @router.message(Command("sponsors"))
    async def sponsors_command(message: Message) -> None:
        if message.from_user is None or message.from_user.id not in countdown_admin_ids:
            await message.answer("این فرمان فقط برای مدیر ربات فعال است. 🔐")
            return
        await message.answer(
            "📣 <b>مدیریت اسپانسرها</b>",
            reply_markup=sponsor_admin_keyboard(await service.all_sponsors()),
        )

    @router.message(Command("stats"))
    async def stats_command(message: Message) -> None:
        if message.from_user is None:
            return
        await service.save_user(telegram_user(message.from_user))
        user = await service.get_user(message.from_user.id)
        stats = await service.get_stats(message.from_user.id)
        await message.answer(
            stats_text(
                user.display_name if user else message.from_user.full_name,
                stats.games_played,
                stats.wins,
                stats.losses,
                stats.correct_guesses,
                stats.wrong_guesses,
                stats.points_won,
            ),
            reply_markup=main_menu(message.from_user.id),
        )

    @router.message(Command("leaderboard"))
    async def leaderboard_command(message: Message) -> None:
        if message.from_user is None:
            return
        await service.save_user(telegram_user(message.from_user))
        await message.answer(
            leaderboard_text(
                await service.leaderboard(3),
                await service.leaderboard_position(message.from_user.id),
            ),
            reply_markup=main_menu(message.from_user.id),
        )

    @router.message(Command("games"))
    async def games_command(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        games = await service.active_games(message.from_user.id)
        if not games:
            await message.answer(
                "فعلاً بازی ناتمامی نداری. یک بازی تازه شروع کن! 🎮",
                reply_markup=main_menu(message.from_user.id),
            )
            return
        await message.answer(f"🔄 <b>{len(games)} بازی ناتمام</b> پیدا شد.\nوضعیت هر بازی:")
        for game in games:
            await send_game_view(bot, game, message.from_user.id, fresh=True)

    @router.message(Command("lastgame"))
    async def last_game_command(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        game = await service.latest_finished_game(message.from_user.id)
        if game is None:
            await message.answer(
                "هنوز نتیجه‌ای برای نمایش نداری. اولین بازی‌ات را شروع کن! 🎮",
                reply_markup=main_menu(message.from_user.id),
            )
            return
        text, keyboard = await view_for(service, bot, game, message.from_user.id)
        await message.answer(
            f"🏁 <b>آخرین نتیجهٔ تو</b>\n\n{text}",
            reply_markup=keyboard or main_menu(message.from_user.id),
        )

    @router.callback_query(F.data == "menu:home")
    async def menu_home(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        games = await service.active_games(callback.from_user.id)
        await safe_edit(
            callback,
            START_TEXT,
            main_menu(callback.from_user.id, bool(games)),
        )
        await callback.answer()

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(F.data == "menu:last")
    async def menu_last_game(callback: CallbackQuery, bot: Bot) -> None:
        game = await service.latest_finished_game(callback.from_user.id)
        if game is None:
            await safe_edit(
                callback,
                "هنوز نتیجه‌ای برای نمایش نداری. اولین بازی‌ات را شروع کن! 🎮",
                main_menu(callback.from_user.id),
            )
        else:
            text, keyboard = await view_for(service, bot, game, callback.from_user.id)
            await safe_edit(
                callback,
                f"🏁 <b>آخرین نتیجهٔ تو</b>\n\n{text}",
                keyboard or main_menu(callback.from_user.id),
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:help")
    async def menu_help(callback: CallbackQuery) -> None:
        await safe_edit(callback, HELP_TEXT, main_menu(callback.from_user.id))
        await callback.answer()

    @router.callback_query(F.data == "menu:support")
    async def menu_support(callback: CallbackQuery) -> None:
        await safe_edit(callback, SUPPORT_TEXT, main_menu(callback.from_user.id))
        await callback.answer()

    @router.callback_query(F.data == "menu:profile")
    async def menu_profile(callback: CallbackQuery) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.get_user(callback.from_user.id)
        if user is None:
            await callback.answer("پروفایل پیدا نشد؛ دوباره /start را بزن.", show_alert=True)
            return
        await safe_edit(
            callback,
            profile_name_text(user),
            profile_name_keyboard(user.nickname_is_custom),
        )
        await callback.answer()

    @router.callback_query(F.data == "profile:name:change")
    async def profile_name_change(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ProfileSetup.waiting_for_name)
        await safe_edit(
            callback,
            "✏️ <b>تغییر نام نمایشی</b>\n\n"
            "نامی را که می‌خواهی در بازی‌ها دیده شود بفرست.\n"
            "نام باید بین ۱ تا ۴۰ کاراکتر باشد. برای لغو: /cancel",
        )
        await callback.answer()

    @router.message(ProfileSetup.waiting_for_name, Command("cancel"))
    async def profile_name_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = await service.get_user(message.from_user.id) if message.from_user else None
        if user is None:
            return
        await message.answer(
            "تغییر نام لغو شد.\n\n" + profile_name_text(user),
            reply_markup=profile_name_keyboard(user.nickname_is_custom),
        )

    @router.message(ProfileSetup.waiting_for_name, F.text)
    async def profile_name_save(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        cleaned = " ".join(message.text.split())
        if not 1 <= len(cleaned) <= 40:
            await message.answer("نام باید بین ۱ تا ۴۰ کاراکتر باشد؛ دوباره بفرست.")
            return
        user = await service.set_user_nickname(message.from_user.id, cleaned)
        if user is None:
            await state.clear()
            await message.answer("پروفایل پیدا نشد؛ دوباره /start را بزن.")
            return
        await state.clear()
        await message.answer(
            "✅ نام نمایشی ذخیره شد.\n\n" + profile_name_text(user),
            reply_markup=profile_name_keyboard(user.nickname_is_custom),
        )

    @router.message(ProfileSetup.waiting_for_name)
    async def profile_name_invalid(message: Message) -> None:
        await message.answer("نام را به‌صورت متن بفرست؛ حداکثر ۴۰ کاراکتر.")

    @router.callback_query(F.data == "profile:name:reset")
    async def profile_name_reset(callback: CallbackQuery) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.set_user_nickname(callback.from_user.id, None)
        if user is None:
            await callback.answer("پروفایل پیدا نشد.", show_alert=True)
            return
        await safe_edit(
            callback,
            "✅ نام نمایشی به نام حساب تلگرام برگشت.\n\n" + profile_name_text(user),
            profile_name_keyboard(False),
        )
        await callback.answer("نام تلگرام فعال شد. ✅")

    @router.callback_query(F.data == "menu:stats")
    async def menu_stats(callback: CallbackQuery) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.get_user(callback.from_user.id)
        stats = await service.get_stats(callback.from_user.id)
        text = stats_text(
            user.display_name if user else callback.from_user.full_name,
            stats.games_played,
            stats.wins,
            stats.losses,
            stats.correct_guesses,
            stats.wrong_guesses,
            stats.points_won,
        )
        await safe_edit(callback, text, main_menu(callback.from_user.id))
        await callback.answer()

    @router.callback_query(F.data == "menu:leaderboard")
    async def menu_leaderboard(callback: CallbackQuery) -> None:
        await service.save_user(telegram_user(callback.from_user))
        top_players = await service.leaderboard(3)
        current_player = await service.leaderboard_position(callback.from_user.id)
        await safe_edit(
            callback,
            leaderboard_text(top_players, current_player),
            main_menu(callback.from_user.id),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:leaderboard:all_time")
    async def menu_all_time_leaderboard(callback: CallbackQuery) -> None:
        await service.save_user(telegram_user(callback.from_user))
        top_players = await service.all_time_leaderboard(3)
        current_player = await service.all_time_leaderboard_position(callback.from_user.id)
        await safe_edit(
            callback,
            leaderboard_text(top_players, current_player, all_time=True),
            main_menu(callback.from_user.id),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:resume")
    async def resume_games(callback: CallbackQuery, bot: Bot) -> None:
        games = await service.active_games(callback.from_user.id)
        if not games:
            await safe_edit(
                callback,
                "بازی ناتمامی پیدا نشد. ✅",
                main_menu(callback.from_user.id),
            )
        else:
            await safe_edit(
                callback,
                f"🔄 <b>{len(games)} بازی بازیابی شد.</b> وضعیت بازی‌ها را پایین می‌بینی.",
                main_menu(callback.from_user.id, True),
            )
            for game in games:
                prefix = None
                if game.is_solo:
                    advance = await service.advance_bot(game.id)
                    game = advance.game
                    prefix = "\n".join(advance.messages[-8:]) or None
                await send_game_view(bot, game, callback.from_user.id, prefix, fresh=True)
        await callback.answer()
