"""Group multiplayer lobby, controls, and private secret/guess inputs."""

from __future__ import annotations

import logging
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..game import GameService
from ..models import GameType
from ..services.group_games import GroupSession, GroupSessionError
from .shared import safe_edit, telegram_user
from .sponsors import check_sponsors

logger = logging.getLogger(__name__)
GROUP_TYPES = {
    "rps": (GameType.ROCK_PAPER_SCISSORS, "✊✋✌️ سنگ، کاغذ، قیچی"),
    "gol": (GameType.GOL_YA_POOCH, "🌸 گل یا پوچ"),
    "ttt": (GameType.TIC_TAC_TOE, "❌⭕ دوز سه‌تایی"),
    "word": (GameType.WORD_GUESS, "🔤 حدس کلمه"),
    "mastermind": (GameType.MASTERMIND, "🎨 فکر بکر"),
    "tod": (GameType.TRUTH_OR_DARE, "🎭 جرئت یا حقیقت"),
}
NAME_PATTERN = re.compile(r"(?<!\w)بازی[\s\-_]*چی(?!\w)")
GROUP_CHAT_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
ERROR_TEXT = {
    "open": "در این گروه یک مسابقهٔ باز هست؛ اول همان را تمام یا لغو کنید.",
    "missing": "مسابقه پیدا نشد.",
    "setup": "تنظیمات مسابقه معتبر نیست.",
    "started": "ثبت‌نام این مسابقه تمام شده است.",
    "joined": "قبلاً در این مسابقه حاضری.",
    "owner": "این کار را فقط شروع‌کنندهٔ مسابقه می‌تواند انجام دهد.",
    "players": "برای شروع، دست‌کم دو عضو باید «حاضرم!» را بزنند.",
    "stale": "این دکمه مربوط به دور فعلی نیست.",
    "member": "اول در مسابقهٔ گروهی ثبت‌نام کن.",
    "choice": "این انتخاب معتبر نیست.",
    "already": "انتخابت برای این دور قبلاً ثبت شده است.",
    "turn": "الان نوبت تو یا مرحلهٔ این انتخاب نیست.",
    "text": "برای این بازی از دکمه یا دستور درست استفاده کن.",
    "word": "کلمه باید فقط شامل ۲ تا ۲۰ حرف و بدون فاصله باشد.",
    "length": "تعداد حروف حدس باید با کلمهٔ مخفی برابر باشد.",
    "code": "چهار رنگ را با ویرگول جدا کن؛ مثلاً 🔵,🟡,⚫,⚪.",
    "answer": "پاسخ باید بین ۱ تا ۱۰۰۰ کاراکتر باشد.",
}


def mentions_bot_name(text: str | None) -> bool:
    if not text:
        return False
    normalized = text.replace("ي", "ی").replace("ك", "ک")
    normalized = re.sub(r"[\u200c\u200d\u2060ـ]", "", normalized)
    return NAME_PATTERN.search(normalized) is not None


def group_menu() -> object:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏆 ساخت مسابقهٔ گروهی", callback_data="grp:new")
    return builder.as_markup()


def _name(session: GroupSession, user_id: int) -> str:
    return escape(next(player.display_name for player in session.players if player.user_id == user_id))


def _private_instruction(session: GroupSession) -> str:
    command = "gword" if session.game_type is GameType.WORD_GUESS else "gcode"
    suffix = (
        "و بعد از یک فاصله، کلمهٔ دلخواهت را بنویس."
        if command == "gword"
        else "و بعد از یک فاصله، چهار رنگ را با ویرگول جدا کن."
    )
    return f"در خصوصی <code>/{command} {session.id}</code> {suffix}"


def session_card(
    session: GroupSession, bot_username: str | None = None
) -> tuple[str, object]:
    title = next(label for kind, label in GROUP_TYPES.values() if kind is session.game_type)
    ranked = sorted(session.players, key=lambda p: (-p.score, p.joined_at, p.user_id))
    scores = "\n".join(
        f"{index}. {escape(player.display_name)}: <b>{player.score}</b>"
        for index, player in enumerate(ranked[:12], 1)
    )
    if len(ranked) > 12:
        scores += f"\n… و {len(ranked) - 12} نفر دیگر"
    text = (
        f"🏆 <b>مسابقهٔ گروهی #{session.id}</b> | {title}\n"
        f"👥 {len(session.players)} بازیکن"
        + (f" | دور {session.current_round} از {session.total_rounds}" if session.status == "active" else "")
        + f"\n\n<b>امتیازها</b>\n{scores}"
    )
    if session.last_result:
        text += f"\n\n{escape(session.last_result)}"
    builder = InlineKeyboardBuilder()

    if session.status == "waiting":
        text += "\n\n⏳ اعضا «حاضرم!» را بزنند؛ سپس شروع‌کننده مسابقه را آغاز کند."
        builder.button(text="🙋 حاضرم!", callback_data=f"grp:join:{session.id}")
        builder.button(text="▶️ شروع بازی", callback_data=f"grp:start:{session.id}")
        builder.button(text="🛑 لغو", callback_data=f"grp:cancel:{session.id}")
        if bot_username:
            builder.button(
                text="📩 ورود به ربات و ثبت دعوت",
                url=f"https://t.me/{bot_username}?start=group_{session.id}",
            )
    elif session.status == "active":
        move = f"grp:move:{session.id}:{session.current_round}:"
        if session.game_type is GameType.ROCK_PAPER_SCISSORS:
            text += (
                f"\n\n🎲 {session.submitted_count} نفر از {len(session.players)} نفر "
                "انتخاب کرده‌اند. حرکت‌ها تا پایان دست مخفی‌اند."
            )
            for value, label in (("rock", "✊ سنگ"), ("paper", "✋ کاغذ"), ("scissors", "✌️ قیچی")):
                builder.button(text=label, callback_data=move + value)
            builder.adjust(3)
        elif session.game_type is GameType.GOL_YA_POOCH:
            if session.phase == "hide":
                text += f"\n\n🌸 {_name(session, session.role_id)} گل را در یکی از ۳ مشت پنهان کند."
            else:
                text += (
                    f"\n\n🌸 گل پنهان شد؛ {session.submitted_count} نفر از "
                    f"{len(session.players) - 1} نفر حدس زده‌اند."
                )
            for fist in range(1, 4):
                builder.button(text=f"✊ مشت {fist}", callback_data=move + str(fist))
            builder.adjust(3)
        elif session.game_type is GameType.TIC_TAC_TOE:
            first, second = session.pair
            text += (
                f"\n\n❌ {_name(session, first.user_id)} × ⭕ {_name(session, second.user_id)}\n"
                f"نوبت: {_name(session, session.turn_id)}"
            )
            for cell, mark in enumerate(session.board):
                builder.button(
                    text={"X": "❌", "O": "⭕", ".": "⬜"}[mark],
                    callback_data=move + str(cell),
                )
            builder.adjust(3, 3, 3)
        elif session.game_type in (GameType.WORD_GUESS, GameType.MASTERMIND):
            role = _name(session, session.role_id)
            if session.phase == "secret":
                text += f"\n\n🔐 {role} راز این دور را بسازد. {_private_instruction(session)}"
            else:
                hint = (
                    f"کلمهٔ {len(session.secret_choice or '')} حرفی است. "
                    if session.game_type is GameType.WORD_GUESS
                    else "کد ۴ رنگ دارد. "
                )
                text += (
                    f"\n\n🔎 راز ثبت شد؛ {session.submitted_count} نفر از "
                    f"{len(session.players) - 1} نفر حدس‌هایشان را تمام کرده‌اند. "
                    f"{hint}"
                    f"{_private_instruction(session)}"
                )
            if bot_username:
                builder.button(
                    text="📩 باز کردن خصوصی ربات",
                    url=f"https://t.me/{bot_username}?start=group_{session.id}",
                )
        else:
            role = _name(session, session.role_id)
            if session.phase == "vote":
                text += (
                    f"\n\n🎭 نوبت {role} است؛ بقیه بین حقیقت و جرئت رأی بدهند. "
                    f"{session.submitted_count} رأی از {len(session.players) - 1} ثبت شده."
                )
                builder.button(text="🗣 حقیقت", callback_data=move + "truth")
                builder.button(text="🔥 جرئت", callback_data=move + "dare")
            elif session.phase == "response":
                text += (
                    f"\n\n🎭 {role} پاسخ دهد:\n<b>{escape(session.prompt_text or '')}</b>\n"
                    f"پاسخت را در گروه با <code>/ganswer {session.id} پاسخ من</code> بفرست."
                )
            else:
                text += (
                    f"\n\n🎭 پاسخ {role}: <b>{escape(session.answer_text or '')}</b>\n"
                    f"{session.submitted_count} رأی از {len(session.players) - 1} ثبت شده؛ "
                    "بقیه پاسخ را داوری کنند."
                )
                builder.button(text="✅ تأیید", callback_data=move + "yes")
                builder.button(text="❌ رد", callback_data=move + "no")
        builder.button(text="🛑 لغو توسط شروع‌کننده", callback_data=f"grp:cancel:{session.id}")
    elif session.status == "finished":
        high = max(player.score for player in session.players)
        winners = [_name(session, p.user_id) for p in session.players if p.score == high]
        text += f"\n\n🏁 پایان مسابقه؛ برنده: <b>{'، '.join(winners[:10])}</b>"
        if len(winners) > 10:
            text += f" و {len(winners) - 10} نفر دیگر"
    else:
        text += "\n\n🛑 مسابقه لغو شد."
    return text, builder.as_markup() if list(builder.buttons) else None


async def refresh_session_card(service: GameService, bot: Bot, session: GroupSession) -> None:
    me = await bot.get_me()
    text, keyboard = session_card(session, me.username)
    try:
        await bot.edit_message_text(
            text, chat_id=session.chat_id, message_id=session.message_id,
            reply_markup=keyboard,
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            logger.warning("Could not update group session %s: %s", session.id, error)
    except TelegramAPIError as error:
        logger.warning("Could not update group session %s: %s", session.id, error)


def register_private_handlers(router: Router, service: GameService) -> None:
    @router.message(Command("gword", "gcode", "ganswer"))
    async def group_private_input(message: Message, command: CommandObject, bot: Bot) -> None:
        if message.from_user is None:
            return
        kind = {"gword": "word", "gcode": "code", "ganswer": "answer"}[command.command]
        try:
            id_text, text = (command.args or "").split(maxsplit=1)
            session_id = int(id_text)
        except ValueError:
            await message.answer("شناسه و متن را بفرست؛ مثلاً <code>/gword 12 کتاب</code>.")
            return
        try:
            session, feedback = await service.group_games.text_input(
                session_id, message.from_user.id, kind, text
            )
        except GroupSessionError as error:
            await message.answer(ERROR_TEXT.get(str(error), ERROR_TEXT["stale"]))
            return
        await message.answer(escape(feedback))
        await refresh_session_card(service, bot, session)


def build_group_router(
    service: GameService, *, admin_ids: frozenset[int] = frozenset()
) -> Router:
    router = Router(name="group_games")
    router.message.filter(F.chat.type.in_(GROUP_CHAT_TYPES))
    router.callback_query.filter(F.message.chat.type.in_(GROUP_CHAT_TYPES))

    @router.my_chat_member()
    async def bot_added_to_group(event: ChatMemberUpdated, bot: Bot) -> None:
        if event.chat.type not in GROUP_CHAT_TYPES:
            return
        before = event.old_chat_member.status
        after = event.new_chat_member.status
        present = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
        if before in present and after not in present:
            await service.referrals.remove_group_owner(event.chat.id)
            return
        if before in present or after not in present:
            return
        actor = event.from_user
        account = await service.get_user(actor.id) if actor and not actor.is_bot else None
        if actor is None or (actor.id not in admin_ids and (account is None or not account.is_activated)):
            try:
                await bot.send_message(
                    event.chat.id,
                    "⭐ برای افزودن بازی‌چی به گروه، دعوت‌کننده باید اشتراک ویژه داشته باشد.",
                )
            except TelegramAPIError:
                pass
            await bot.leave_chat(event.chat.id)
            return
        await service.referrals.set_group_owner(event.chat.id, actor.id)
        bot_user = await bot.get_me()
        builder = InlineKeyboardBuilder()
        builder.button(
            text="👥 ورود به بازی‌چی",
            url=f"https://t.me/{bot_user.username}?start=grpchat_{event.chat.id}",
        )
        try:
            await bot.send_message(
                event.chat.id,
                "👋 بازی‌چی به گروه اضافه شد! اعضایی که از دکمهٔ زیر وارد ربات شوند "
                "به‌عنوان دعوت‌شدهٔ افزودن‌کنندهٔ ربات ثبت می‌شوند.",
                reply_markup=builder.as_markup(),
            )
        except TelegramAPIError as error:
            logger.warning("Could not announce group referral link in %s: %s", event.chat.id, error)

    async def allowed(callback: CallbackQuery, bot: Bot) -> bool:
        account = await service.get_user(callback.from_user.id)
        if account is None or (not account.is_activated and account.telegram_id not in admin_ids):
            await callback.answer("برای مسابقهٔ گروهی به اشتراک ویژه نیاز داری؛ ربات را در خصوصی باز کن.", show_alert=True)
            return False
        sponsors = await service.active_sponsors()
        if sponsors:
            check = await check_sponsors(bot, callback.from_user.id, sponsors)
            if not check.allowed:
                await callback.answer(
                    "اول عضویت‌های لازم را در گفت‌وگوی خصوصی کامل کن.", show_alert=True
                )
                return False
        return True

    async def bound(callback: CallbackQuery, session_id: int) -> GroupSession | None:
        try:
            session = await service.group_games.get(session_id)
        except GroupSessionError:
            await callback.answer(ERROR_TEXT["missing"], show_alert=True)
            return None
        if (session.chat_id, session.message_id) != (
            callback.message.chat.id, callback.message.message_id
        ):
            await callback.answer("این دکمه برای مسابقهٔ این گروه نیست.", show_alert=True)
            return None
        return session

    async def show(callback: CallbackQuery, bot: Bot, session: GroupSession) -> None:
        username = (await bot.get_me()).username
        text, keyboard = session_card(session, username)
        await safe_edit(callback, text, keyboard)

    @router.message(Command("play"))
    async def group_play(message: Message) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        account = await service.get_user(message.from_user.id)
        if message.from_user.id not in admin_ids and (account is None or not account.is_activated):
            await message.answer("⭐ مسابقهٔ گروهی ویژهٔ مشترکان است. برای خرید، ربات را در خصوصی باز کن.")
            return
        existing = await service.group_games.active_in_chat(message.chat.id)
        if existing:
            await message.answer(
                f"🎮 مسابقهٔ گروهی #{existing.id} باز است؛ از کارت همان مسابقه استفاده کنید."
            )
            return
        await message.answer(
            "🎮 <b>بازی‌چی اینجاست!</b> یک مسابقهٔ چندنفره بسازید تا اعضای گروه "
            "همگی در آن بازی کنند.", reply_markup=group_menu(),
        )

    @router.message(Command("ganswer"))
    async def group_answer(message: Message, command: CommandObject, bot: Bot) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        try:
            id_text, answer = (command.args or "").split(maxsplit=1)
            session_id = int(id_text)
            session = await service.group_games.get(session_id)
        except (ValueError, GroupSessionError):
            await message.answer("مثلاً بفرست: <code>/ganswer 12 پاسخ من</code>")
            return
        if session.chat_id != message.chat.id:
            await message.answer("این مسابقه در این گروه نیست.")
            return
        account = await service.get_user(message.from_user.id)
        if message.from_user.id not in admin_ids and (account is None or not account.is_activated):
            await message.answer("برای مسابقهٔ گروهی به اشتراک ویژه نیاز داری.")
            return
        if message.from_user.id not in admin_ids:
            sponsors = await service.active_sponsors()
            if sponsors and not (await check_sponsors(bot, message.from_user.id, sponsors)).allowed:
                await message.answer("اول عضویت‌های لازم را در گفت‌وگوی خصوصی کامل کن.")
                return
        try:
            session, feedback = await service.group_games.text_input(
                session_id, message.from_user.id, "answer", answer
            )
        except GroupSessionError as error:
            await message.answer(ERROR_TEXT.get(str(error), ERROR_TEXT["stale"]))
            return
        await message.answer(feedback)
        await refresh_session_card(service, bot, session)

    @router.message(F.text | F.caption)
    async def group_name(message: Message) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        text = message.text or message.caption or ""
        if text.startswith("/") or not mentions_bot_name(text):
            return
        account = await service.get_user(message.from_user.id)
        if message.from_user.id not in admin_ids and (account is None or not account.is_activated):
            await message.answer("👋 من اینجام! مسابقهٔ گروهی با اشتراک ویژه باز می‌شود؛ در خصوصی ربات اشتراکت را فعال کن. ⭐")
            return
        existing = await service.group_games.active_in_chat(message.chat.id)
        if existing:
            await message.answer(
                f"👋 من اینجام! مسابقهٔ #{existing.id} در گروه باز است؛ "
                "از کارت مسابقه به آن بپیوندید. 🎲"
            )
            return
        await message.answer(
            "👋 من اینجام! بیا یک مسابقهٔ گروهی شروع کنیم؛ همهٔ اعضا می‌تونن «حاضرم!» بزنن. 🎲",
            reply_markup=group_menu(),
        )

    @router.callback_query(F.data == "grp:new")
    async def group_new(callback: CallbackQuery, bot: Bot) -> None:
        if not await allowed(callback, bot):
            return
        if await service.group_games.active_in_chat(callback.message.chat.id):
            await callback.answer(ERROR_TEXT["open"], show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        for code, (_, label) in GROUP_TYPES.items():
            builder.button(text=label, callback_data=f"grp:type:{callback.from_user.id}:{code}")
        builder.adjust(2)
        await safe_edit(
            callback,
            f"🏆 <b>{escape(callback.from_user.full_name)}</b>، مسابقهٔ گروهی کدام بازی باشد؟",
            builder.as_markup(),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("grp:type:"))
    async def group_type(callback: CallbackQuery, bot: Bot) -> None:
        try:
            _, _, owner_text, code = (callback.data or "").split(":")
            owner_id = int(owner_text)
            if code not in GROUP_TYPES:
                raise ValueError
        except ValueError:
            await callback.answer(ERROR_TEXT["setup"], show_alert=True)
            return
        if callback.from_user.id != owner_id:
            await callback.answer(ERROR_TEXT["owner"], show_alert=True)
            return
        if not await allowed(callback, bot):
            return
        await service.save_user(telegram_user(callback.from_user))
        try:
            session = await service.group_games.create(
                callback.message.chat.id, callback.message.message_id,
                owner_id, callback.from_user.full_name, GROUP_TYPES[code][0],
            )
        except GroupSessionError as error:
            await callback.answer(ERROR_TEXT.get(str(error), ERROR_TEXT["setup"]), show_alert=True)
            return
        await show(callback, bot, session)
        await callback.answer("منتظر اعلام آمادگی اعضا هستیم ✅")

    @router.callback_query(F.data.startswith("grp:join:"))
    async def group_join(callback: CallbackQuery, bot: Bot) -> None:
        try:
            session_id = int((callback.data or "").rsplit(":", 1)[-1])
        except ValueError:
            await callback.answer(ERROR_TEXT["missing"], show_alert=True)
            return
        session = await bound(callback, session_id)
        if session is None:
            return
        if session.status != "waiting":
            await callback.answer(ERROR_TEXT["started"], show_alert=True)
            return
        await service.save_user(telegram_user(callback.from_user))
        await service.referrals.claim(callback.from_user.id, session.creator_id, "group")
        if not await allowed(callback, bot):
            return
        try:
            session = await service.group_games.join(
                session_id, callback.from_user.id, callback.from_user.full_name
            )
        except GroupSessionError as error:
            await callback.answer(ERROR_TEXT.get(str(error), ERROR_TEXT["stale"]), show_alert=True)
            return
        await show(callback, bot, session)
        await callback.answer("حاضر شدی ✅")

    @router.callback_query(F.data.startswith("grp:start:"))
    async def group_start(callback: CallbackQuery, bot: Bot) -> None:
        try:
            session_id = int((callback.data or "").rsplit(":", 1)[-1])
        except ValueError:
            await callback.answer(ERROR_TEXT["missing"], show_alert=True)
            return
        if await bound(callback, session_id) is None or not await allowed(callback, bot):
            return
        try:
            session = await service.group_games.start(session_id, callback.from_user.id)
        except GroupSessionError as error:
            await callback.answer(ERROR_TEXT.get(str(error), ERROR_TEXT["stale"]), show_alert=True)
            return
        await show(callback, bot, session)
        await callback.answer("مسابقه شروع شد 🎲")

    @router.callback_query(F.data.startswith("grp:move:"))
    async def group_move(callback: CallbackQuery, bot: Bot) -> None:
        try:
            _, _, session_text, round_text, choice = (callback.data or "").split(":")
            session_id, round_number = int(session_text), int(round_text)
        except ValueError:
            await callback.answer(ERROR_TEXT["choice"], show_alert=True)
            return
        if await bound(callback, session_id) is None or not await allowed(callback, bot):
            return
        try:
            session = await service.group_games.move(
                session_id, round_number, callback.from_user.id, choice
            )
        except GroupSessionError as error:
            await callback.answer(ERROR_TEXT.get(str(error), ERROR_TEXT["stale"]), show_alert=True)
            return
        await show(callback, bot, session)
        await callback.answer("انتخابت ثبت شد ✅")

    @router.callback_query(F.data.startswith("grp:cancel:"))
    async def group_cancel(callback: CallbackQuery, bot: Bot) -> None:
        try:
            session_id = int((callback.data or "").rsplit(":", 1)[-1])
        except ValueError:
            await callback.answer(ERROR_TEXT["missing"], show_alert=True)
            return
        if await bound(callback, session_id) is None or not await allowed(callback, bot):
            return
        try:
            session = await service.group_games.cancel(session_id, callback.from_user.id)
        except GroupSessionError as error:
            await callback.answer(ERROR_TEXT.get(str(error), ERROR_TEXT["stale"]), show_alert=True)
            return
        await show(callback, bot, session)
        await callback.answer("مسابقه لغو شد")

    return router
