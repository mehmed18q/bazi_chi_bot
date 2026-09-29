"""Aiogram handlers for private-chat setup, invitations, and gameplay."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from aiogram.types import User as TelegramUser

from ..game import (
    CannotJoinOwnGame,
    GameError,
    GameNotFound,
    GameService,
    InvalidCell,
    InvalidFinalChoice,
    InvalidFinalMessage,
    InvalidFist,
    InvalidWord,
    InvalidWordLength,
    InviteUnavailable,
    NotAPlayer,
    NotYourTurn,
    PendingFinalResponse,
    RepeatedQuestion,
    StaleAction,
)
from ..models import Game, MastermindGuessResult, TurnResult, User, WordGuessResult
from ..rules import MASTERMIND_COLOR_EMOJIS
from ..ui import (
    FINAL_LABELS,
    game_keyboard,
    render_game,
    word_guess_board,
    mastermind_board,
)

logger = logging.getLogger(__name__)


class CountdownSetup(StatesGroup):
    waiting_for_datetime = State()


class QuestionSetup(StatesGroup):
    waiting_for_kind = State()
    waiting_for_text = State()


class SponsorSetup(StatesGroup):
    waiting_for_details = State()


class AdminMessageSetup(StatesGroup):
    waiting_for_text = State()


class ProfileSetup(StatesGroup):
    waiting_for_name = State()


ERROR_MESSAGES: dict[type[GameError], str] = {
    GameNotFound: "این بازی پیدا نشد؛ لینک را دوباره بررسی کن. 🔍",
    CannotJoinOwnGame: "این لینک برای خودته! آن را برای هم‌بازی‌ات بفرست. 🙂",
    InviteUnavailable: "این لینک قبلاً استفاده شده یا بازی دیگر قابل ورود نیست. ⏳",
    NotAPlayer: "تو بازیکن این مسابقه نیستی. 🚫",
    NotYourTurn: "الان نوبت تو نیست؛ کمی صبر کن. ⏳",
    InvalidFist: "شمارهٔ مشت معتبر نیست. 🤔",
    InvalidCell: "این خانه پر است یا انتخابت معتبر نیست؛ یک خانهٔ خالی را بزن. ❌⭕",
    InvalidWord: "کلمه باید بدون فاصله، فقط شامل حروف و بین ۲ تا ۲۰ حرف باشد. 🔤",
    InvalidWordLength: "تعداد حروف حدس باید دقیقاً با کلمهٔ مخفی برابر باشد. 🔢",
    InvalidFinalChoice: "انتخاب نهایی معتبر نیست.",
    InvalidFinalMessage: "پیام باید بین ۱ تا ۳۰۰۰ کاراکتر باشد. ✍️",
    RepeatedQuestion: ("این سؤال قبلاً برای همین پاسخ‌دهنده در برابر تو مطرح شده؛ سؤال دیگری بنویس."),
    PendingFinalResponse: ("اول به سؤال قبلی‌ات پاسخ بده؛ از «ادامهٔ بازی‌ها» می‌توانی آن را ببینی."),
    StaleAction: "این دکمه قبلاً استفاده شده؛ وضعیت تازه را می‌بینی. 🔄",
}


def telegram_user(user: TelegramUser) -> User:
    return User(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        display_name=user.full_name,
    )


def profile_name_text(user: User) -> str:
    telegram_name = user.telegram_display_name or user.display_name
    custom_note = (
        "\nاین نام را خودت انتخاب کرده‌ای."
        if user.nickname_is_custom
        else "\nفعلاً نام حساب تلگرامت در بازی‌ها نمایش داده می‌شود."
    )
    return (
        "👤 <b>نام نمایشی تو</b>\n\n"
        f"نام داخل ربات: <b>{escape(user.display_name)}</b>\n"
        f"نام حساب تلگرام: <b>{escape(telegram_name)}</b>"
        f"{custom_note}\n\n"
        "می‌خواهی همین نام بماند یا آن را تغییر بدهی؟"
    )


async def refresh_profile_photo(service: GameService, bot: Bot, user_id: int) -> str | None:
    """Persist Telegram's reusable file_id; download URLs are temporary and expose the token."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
    except TelegramAPIError:
        return None
    file_id = photos.photos[0][-1].file_id if photos.total_count and photos.photos else None
    await service.set_user_profile_photo(user_id, file_id)
    return file_id


async def invite_url(bot: Bot, game: Game) -> str:
    bot_user = await bot.get_me()
    if not bot_user.username:
        raise RuntimeError("Telegram bot has no username; deep links cannot be created")
    return f"https://t.me/{bot_user.username}?start=join_{game.invite_token}"


async def game_names(service: GameService, game: Game) -> dict[int, str]:
    ids = [game.creator_id]
    if game.player2_id is not None:
        ids.append(game.player2_id)
    names: dict[int, str] = {}
    for user_id in ids:
        user = await service.get_user(user_id)
        names[user_id] = user.display_name if user else f"بازیکن {user_id}"
    return names


async def view_for(
    service: GameService, bot: Bot, game: Game, viewer_id: int
) -> tuple[str, object]:
    names = await game_names(service, game)
    link = await invite_url(bot, game) if game.status.value == "waiting" else None
    return render_game(game, viewer_id, names, link), game_keyboard(game, viewer_id, link)


async def safe_edit(callback: CallbackQuery, text: str, reply_markup: object = None) -> bool:
    if callback.message is None:
        return False
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return True
        logger.warning("Could not edit game message: %s", error)
        return False
    return True


async def safe_send(bot: Bot, chat_id: int, text: str, reply_markup: object = None) -> Any | None:
    try:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        logger.warning("Player %s blocked or stopped the bot", chat_id)
    except TelegramAPIError as error:
        logger.warning("Could not send a message to player %s: %s", chat_id, error)
    return None


_notification_tasks: set[asyncio.Task[None]] = set()


async def _delete_turn_notification(bot: Bot, chat_id: int, message_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        logger.debug("Could not remove turn notification %s/%s", chat_id, message_id)


async def notify_turn(
    bot: Bot,
    user_id: int,
    text: str = "🔔 نوبت توست! کارت بازی را باز کن.",
    *,
    delete_after: float = 10,
) -> None:
    """Send a real notification, then remove it so game chats stay tidy."""
    message = await safe_send(bot, user_id, text)
    message_id = getattr(message, "message_id", None)
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return
    task = asyncio.create_task(
        _delete_turn_notification(bot, user_id, message_id, delete_after),
        name=f"turn-notification-{user_id}-{message_id}",
    )
    _notification_tasks.add(task)
    task.add_done_callback(_notification_tasks.discard)


def game_error_text(error: GameError) -> str:
    for error_type, text in ERROR_MESSAGES.items():
        if isinstance(error, error_type):
            return text
    return "این کار الان انجام نشد؛ دوباره تلاش کن. ⚠️"


def round_result_text(result: TurnResult, names: dict[int, str]) -> str:
    point_winner = escape(names.get(result.point_winner_id, "بازیکن"))
    if result.correct:
        outcome = "🎯 <b>حدس درست بود!</b>"
    else:
        outcome = "🛡 <b>حدس اشتباه بود!</b>"
    return (
        f"{outcome}\n"
        f"🌺 گل در <b>مشت {result.hidden_fist}</b> بود.\n"
        f"⭐ این دست برای <b>{point_winner}</b> یک امتیاز داشت."
    )


def word_round_result_text(result: WordGuessResult, names: dict[int, str]) -> str:
    board = word_guess_board(result.guesses)
    if not result.round_finished:
        return f"🔤 <b>نتیجهٔ حدس:</b>\n\n{board}"
    point_winner = escape(names.get(result.point_winner_id, "بازیکن"))
    outcome = (
        "🎯 <b>کلمه درست حدس زده شد!</b>"
        if result.guessed_correctly
        else "⌛ <b>فرصت‌های حدس تمام شد.</b>"
    )
    return (
        f"{outcome}\n"
        f"🔐 کلمه: <b>{escape(result.secret)}</b>\n"
        f"⭐ امتیاز این دور برای <b>{point_winner}</b>\n\n"
        f"{board}"
    )


def mastermind_round_result_text(result: MastermindGuessResult, names: dict[int, str]) -> str:
    board = mastermind_board(result.guesses)
    if not result.round_finished:
        return f"🎨 <b>نتیجهٔ حدس:</b> ⚫ {result.black} | ⚪ {result.white}\n\n{board}"
    point_winner = escape(names.get(result.point_winner_id, "بازیکن"))
    outcome = (
        "🎯 <b>کد را درست حدس زدی!</b>"
        if result.guessed_correctly
        else "⌛ <b>تلاش‌های این دور تمام شد.</b>"
    )
    secret = " ".join(MASTERMIND_COLOR_EMOJIS.get(color, "⚪") for color in result.secret)
    return (
        f"{outcome}\n"
        f"🔐 کد مخفی: <b>{secret}</b>\n"
        f"⭐ امتیاز این دور برای <b>{point_winner}</b>\n"
        f"⚫ مهرهٔ سیاه: <b>{result.black}</b> | ⚪ مهرهٔ سفید: <b>{result.white}</b>\n\n"
        f"{board}"
    )


def final_choice_label(game: Game) -> str:
    return FINAL_LABELS.get(game.final_choice, "انتخاب نامشخص")


class GamePresenter:
    """Render and deliver player-specific views without changing domain state."""

    def __init__(self, service: GameService) -> None:
        self.service = service

    async def send_game_view(
        self, bot: Bot, game: Game, user_id: int, prefix: str | None = None
    ) -> None:
        text, keyboard = await view_for(self.service, bot, game, user_id)
        if prefix:
            text = f"{prefix}\n\n{text}"
        stored = await self.service.game_message(game.id, user_id)
        if stored is not None:
            chat_id, message_id = stored
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=keyboard,
                )
                return
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
                logger.info(
                    "Stored game message %s/%s is no longer editable: %s",
                    chat_id,
                    message_id,
                    error,
                )
            except TelegramForbiddenError:
                logger.warning("Player %s blocked or stopped the bot", user_id)
                return
            except TelegramAPIError as error:
                logger.warning("Could not update game message for player %s: %s", user_id, error)
                return

        message = await safe_send(bot, user_id, text, keyboard)
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            await self.service.save_game_message(game.id, user_id, user_id, message_id)

    async def edit_game_view(
        self, callback: CallbackQuery, bot: Bot, game: Game, prefix: str | None = None
    ) -> None:
        text, keyboard = await view_for(self.service, bot, game, callback.from_user.id)
        if prefix:
            text = f"{prefix}\n\n{text}"
        if await safe_edit(callback, text, keyboard):
            message = callback.message
            message_id = getattr(message, "message_id", None)
            chat = getattr(message, "chat", None)
            chat_id = getattr(chat, "id", callback.from_user.id)
            if (
                isinstance(message_id, int)
                and not isinstance(message_id, bool)
                and isinstance(chat_id, int)
                and not isinstance(chat_id, bool)
            ):
                await self.service.save_game_message(
                    game.id, callback.from_user.id, chat_id, message_id
                )
            return
        await self.send_game_view(bot, game, callback.from_user.id, prefix)
