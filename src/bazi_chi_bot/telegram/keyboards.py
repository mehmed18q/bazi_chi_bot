"""Persian text and native Telegram inline keyboards."""

from __future__ import annotations

from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..models import Game, GamePhase, GameStatus, GameType, User
from ..rules import MASTERMIND_COLOR_EMOJIS, MASTERMIND_COLORS


def menu_keyboard(
    has_active_games: bool = False, is_countdown_admin: bool = False
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎮 شروع بازی", callback_data="menu:new")
    builder.button(text="🎯 چالش روزانه", callback_data="menu:daily_challenge")
    builder.button(text="📊 آمار من", callback_data="menu:stats")
    builder.button(text="👤 نام نمایشی", callback_data="menu:profile")
    if has_active_games:
        builder.button(text="🔄 ادامهٔ بازی", callback_data="menu:resume")
    builder.button(text="🏆 لیست برترین بازیکن‌ها", callback_data="menu:leaderboard")
    builder.button(text="🏅 برترین‌ها در همهٔ دوره‌ها", callback_data="menu:leaderboard:all_time")
    builder.button(text="🏁 آخرین نتیجه", callback_data="menu:last")
    builder.button(text="❔ راهنما", callback_data="menu:help")
    builder.button(text="🛟 پشتیبانی", callback_data="menu:support")
    if is_countdown_admin:
        builder.button(text="⚙️ مدیریت ربات", callback_data="menu:admin")
    builder.adjust(2, 2, 1 if has_active_games else 2, 2, 2, 2, 1)
    return builder.as_markup()


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    """Controls available from the admin-only menu."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💌 شمارش‌معکوس", callback_data="menu:countdown")
    builder.button(text="📝 افزودن سؤال", callback_data="menu:add_question")
    builder.button(text="📣 مدیریت اسپانسرها", callback_data="menu:sponsors")
    builder.button(text="👥 کاربران ربات", callback_data="menu:users")
    builder.button(text="💳 تنظیمات پرداخت", callback_data="menu:payments")
    builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def payment_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 تغییر مبلغ پایه", callback_data="admin_payment:set:amount")
    builder.button(text="💳 تغییر شماره کارت", callback_data="admin_payment:set:card")
    builder.button(text="👤 تغییر صاحب کارت", callback_data="admin_payment:set:holder")
    builder.button(text="↩️ مدیریت ربات", callback_data="menu:admin")
    builder.adjust(1)
    return builder.as_markup()


def activation_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📤 ارسال رسید پرداخت", callback_data="payment:send")
    return builder.as_markup()


def payment_review_keyboard(receipt_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ تأیید و فعال‌سازی", callback_data=f"payment:approve:{receipt_id}")
    builder.button(text="❌ رد رسید", callback_data=f"payment:reject:{receipt_id}")
    builder.adjust(2)
    return builder.as_markup()


def profile_name_keyboard(is_custom: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ تغییر نام", callback_data="profile:name:change")
    if is_custom:
        builder.button(text="🔄 استفاده از نام تلگرام", callback_data="profile:name:reset")
    builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def countdown_users_keyboard(
    users: list[User], page: int = 0, page_size: int = 8
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    page_count = max(1, (len(users) + page_size - 1) // page_size)
    safe_page = min(max(0, page), page_count - 1)
    start = safe_page * page_size
    for user in users[start : start + page_size]:
        username = f" — @{user.username}" if user.username else ""
        label = f"👤 {user.display_name}{username}"
        builder.button(
            text=label[:64],
            callback_data=f"countdown:user:{user.telegram_id}",
        )
    builder.adjust(1)
    if page_count > 1:
        navigation: list[InlineKeyboardButton] = []
        if safe_page > 0:
            navigation.append(
                InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"countdown:page:{safe_page - 1}")
            )
        navigation.append(
            InlineKeyboardButton(text=f"{safe_page + 1}/{page_count}", callback_data="noop")
        )
        if safe_page + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(text="بعدی ➡️", callback_data=f"countdown:page:{safe_page + 1}")
            )
        builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu:home"))
    return builder.as_markup()


def question_kind_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="حقیقت 🗣", callback_data="admin_question:truth")
    builder.button(text="جرئت 🔥", callback_data="admin_question:dare")
    builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
    builder.adjust(2, 1)
    return builder.as_markup()


def sponsor_admin_keyboard(sponsors: list[object]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ افزودن اسپانسر", callback_data="admin_sponsor:add")
    for sponsor in sponsors:
        active = getattr(sponsor, "active")
        sid = getattr(sponsor, "id")
        label = ("✅ " if active else "⏸️ ") + str(getattr(sponsor, "title"))
        builder.button(text=label[:64], callback_data=f"admin_sponsor:toggle:{sid}")
        builder.button(text="🗑 حذف", callback_data=f"admin_sponsor:delete:{sid}")
    builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
    builder.adjust(1, 2)
    return builder.as_markup()


def sponsor_kind_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📣 کانال", callback_data="admin_sponsor:kind:channel")
    builder.button(text="🤖 ربات", callback_data="admin_sponsor:kind:bot")
    builder.button(text="↩️ برگشت", callback_data="menu:sponsors")
    builder.adjust(2, 1)
    return builder.as_markup()


def required_sponsors_keyboard(sponsors: list[object], payload: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sponsor in sponsors:
        icon = "🤖" if getattr(sponsor, "kind") == "bot" else "📣"
        builder.button(
            text=f"{icon} ورود به {getattr(sponsor, 'title')}",
            url=getattr(sponsor, "url"),
        )
    callback_data = f"sponsors:check:{payload}" if payload else "sponsors:check"
    builder.button(text="🔄 بررسی عضویت", callback_data=callback_data)
    builder.adjust(1)
    return builder.as_markup()


def admin_users_keyboard(users: list[User]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user in users:
        status = "✅" if user.is_activated else "🔒"
        internal_id = user.id if user.id is not None else "—"
        builder.button(
            text=f"{status} #{internal_id} — {user.display_name}"[:64],
            callback_data=f"admin_user:view:{user.telegram_id}",
        )
    builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def admin_user_actions(user_id: int, is_activated: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_activated:
        builder.button(
            text="🔒 غیرفعال‌کردن کاربر",
            callback_data=f"admin_user:activation:{user_id}:off",
        )
    else:
        builder.button(
            text="✅ فعال‌کردن کاربر",
            callback_data=f"admin_user:activation:{user_id}:on",
        )
    builder.button(text="✉️ ارسال پیام", callback_data=f"admin_user:message:{user_id}")
    builder.button(text="↩️ فهرست کاربران", callback_data="menu:users")
    builder.adjust(1)
    return builder.as_markup()


def game_types_keyboard(solo: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    prefix = "setup:solo:type:" if solo else "setup:type:"
    builder.button(text="🌸 گل یا پوچ", callback_data=f"{prefix}gol")
    builder.button(text="❌⭕ دوز سه‌تایی", callback_data=f"{prefix}ttt")
    builder.button(text="✊✋✌️ سنگ، کاغذ، قیچی", callback_data=f"{prefix}rps")
    builder.button(text="🔤 حدس کلمه", callback_data=f"{prefix}word")
    builder.button(text="🎨 فکر بکر", callback_data=f"{prefix}mastermind")
    if not solo:
        builder.button(text="🎭 جرئت یا حقیقت", callback_data="setup:type:tod")
    builder.button(text="🎲 بازی شانسی", callback_data=f"{prefix}random")
    if not solo:
        builder.button(text="🤖 بازی تک‌نفره با ربات", callback_data="setup:solo")
    builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def daily_challenge_keyboard(
    *, open_now: bool, reminder_enabled: bool, can_play: bool = True
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if open_now and can_play:
        builder.button(text="🎮 ورود به چالش امروز", callback_data="daily:play")
    builder.button(
        text="🔕 لغو یادآور روزانه" if reminder_enabled else "🔔 فعال‌کردن یادآور روزانه",
        callback_data="daily:reminder:off" if reminder_enabled else "daily:reminder:on",
    )
    builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def board_text(board: str) -> str:
    symbols = {"X": "❌", "O": "⭕", ".": "⬜"}
    return "\n".join(" ".join(symbols[c] for c in board[i : i + 3]) for i in (0, 3, 6))


def fists_keyboard(solo: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for fists in range(2, 7):
        builder.button(text=f"{fists} مشت ✊", callback_data=f"setup:solo:f:{fists}" if solo else f"setup:f:{fists}")
    builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def hands_keyboard(
    fists: int, game_type: GameType = GameType.GOL_YA_POOCH, solo: bool = False
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for hands in (3, 5, 7, 9):
        if solo:
            callback = f"setup:solo:play:{game_type.value}:{fists}:{hands}"
        elif game_type is GameType.GOL_YA_POOCH:
            callback = f"setup:h:{fists}:{hands}"
        else:
            route = {
                GameType.TIC_TAC_TOE: "ttt",
                GameType.ROCK_PAPER_SCISSORS: "rps",
                GameType.TRUTH_OR_DARE: "tod",
                GameType.WORD_GUESS: "word",
                GameType.MASTERMIND: "mastermind",
            }[game_type]
            callback = f"setup:{route}:{hands}"
        unit = (
            "دور"
            if game_type in {GameType.TRUTH_OR_DARE, GameType.WORD_GUESS, GameType.MASTERMIND}
            else "دست"
        )
        builder.button(text=f"{hands} {unit}", callback_data=callback)
    builder.button(text="↩️ تغییر بازی", callback_data="menu:new")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def game_keyboard(
    game: Game, user_id: int, invite_url: str | None = None
) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    if game.status is GameStatus.WAITING and user_id == game.creator_id:
        if invite_url:
            invitation = {
                GameType.GOL_YA_POOCH: "بیا با هم گل یا پوچ بازی کنیم! 🌸",
                GameType.TIC_TAC_TOE: "بیا با هم دوز سه‌تایی بازی کنیم! ❌⭕",
                GameType.ROCK_PAPER_SCISSORS: "بیا سنگ، کاغذ، قیچی بازی کنیم! ✊✋✌️",
                GameType.TRUTH_OR_DARE: "بیا با هم جرئت یا حقیقت بازی کنیم! 🎭",
                GameType.WORD_GUESS: "بیا با هم حدس کلمه بازی کنیم! 🔤",
                GameType.MASTERMIND: "بیا با هم فکر بکر بازی کنیم! 🎨",
            }[game.game_type]
            share_url = (
                "https://t.me/share/url?url="
                f"{quote(invite_url, safe='')}&text={quote(invitation, safe='')}"
            )
            builder.row(InlineKeyboardButton(text="📨 دعوت دوست", url=share_url))
        builder.row(
            InlineKeyboardButton(
                text="🛑 لغو بازی",
                callback_data=f"game:{game.id}:{game.version}:cancel:0",
            )
        )
        return builder.as_markup()

    if game.game_type is GameType.TIC_TAC_TOE and game.status is GameStatus.ACTIVE:
        if game.next_player_id != user_id:
            return None
        for cell, mark in enumerate(game.board):
            builder.button(
                text={"X": "❌", "O": "⭕", ".": "⬜"}[mark],
                callback_data=f"game:{game.id}:{game.version}:move:{cell}",
            )
        builder.adjust(3)
        return builder.as_markup()

    if game.game_type is GameType.ROCK_PAPER_SCISSORS and game.status is GameStatus.ACTIVE:
        if game.next_player_id != user_id:
            return None
        for choice, label in (("rock", "✊ سنگ"), ("paper", "✋ کاغذ"), ("scissors", "✌️ قیچی")):
            builder.button(
                text=label,
                callback_data=f"game:{game.id}:{game.version}:rps:{choice}",
            )
        builder.adjust(3)
        return builder.as_markup()

    if (
        game.game_type is GameType.MASTERMIND
        and game.status is GameStatus.ACTIVE
        and (
            (game.phase is GamePhase.HIDING and game.hider_id == user_id)
            or (game.phase is GamePhase.GUESSING and game.guesser_id == user_id)
        )
    ):
        for color in MASTERMIND_COLORS:
            builder.button(
                text=MASTERMIND_COLOR_EMOJIS[color],
                callback_data=f"game:{game.id}:{game.version}:mastermind:{color}",
            )
        if game.mastermind_draft:
            builder.button(
                text="↩️ پاک‌کردن انتخاب",
                callback_data=f"game:{game.id}:{game.version}:mastermind:reset",
            )
            builder.adjust(3, 3, 1)
        else:
            builder.adjust(3, 3)
        return builder.as_markup()

    if (
        game.game_type is GameType.GOL_YA_POOCH
        and game.phase is GamePhase.HIDING
        and game.hider_id == user_id
    ):
        for fist in range(1, game.fists + 1):
            builder.button(
                text=f"مشت {fist} ✊",
                callback_data=f"game:{game.id}:{game.version}:hide:{fist}",
            )
        builder.adjust(3)
        return builder.as_markup()

    if (
        game.game_type is GameType.GOL_YA_POOCH
        and game.phase is GamePhase.GUESSING
        and game.guesser_id == user_id
    ):
        for fist in range(1, game.fists + 1):
            builder.button(
                text=f"مشت {fist} 🤔",
                callback_data=f"game:{game.id}:{game.version}:guess:{fist}",
            )
        builder.adjust(3)
        return builder.as_markup()

    if game.phase is GamePhase.CHOICE and game.loser_id == user_id:
        builder.button(
            text="حقیقت 🗣",
            callback_data=f"game:{game.id}:{game.version}:final:truth",
        )
        builder.button(
            text="جرئت 🔥",
            callback_data=f"game:{game.id}:{game.version}:final:dare",
        )
        builder.adjust(2)
        return builder.as_markup()

    if (
        game.game_type is GameType.TRUTH_OR_DARE
        and game.phase is GamePhase.CHOICE
        and game.challenge_asker_id == user_id
        and game.challenge_kind is None
    ):
        builder.button(
            text="حقیقت 🗣", callback_data=f"game:{game.id}:{game.version}:challenge:truth"
        )
        builder.button(
            text="جرئت 🔥", callback_data=f"game:{game.id}:{game.version}:challenge:dare"
        )
        builder.adjust(2)
        return builder.as_markup()

    if (
        game.game_type is GameType.TRUTH_OR_DARE
        and game.phase is GamePhase.FINISHED
        and game.challenge_response_text is not None
        and game.challenge_approved is None
        and game.challenge_asker_id == user_id
    ):
        builder.button(
            text="✅ تأیید پاسخ",
            callback_data=f"game:{game.id}:{game.version}:challenge_review:yes",
        )
        builder.button(
            text="❌ رد پاسخ", callback_data=f"game:{game.id}:{game.version}:challenge_review:no"
        )
        builder.adjust(2)
        return builder.as_markup()

    if (
        game.status is GameStatus.FINISHED
        and game.final_response_text is not None
        and game.final_response_approved is None
        and game.winner_id == user_id
    ):
        builder.button(text="✅ تأیید", callback_data=f"game:{game.id}:{game.version}:review:yes")
        builder.button(
            text="❌ عدم تأیید", callback_data=f"game:{game.id}:{game.version}:review:no"
        )
        builder.adjust(2)
        return builder.as_markup()

    return None
