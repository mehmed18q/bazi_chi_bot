"""Aiogram handlers for private-chat setup, invitations, and gameplay."""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.types import Message

from ..game import (
    GameError,
    GameService,
)
from ..models import Game, GamePhase, GameStatus, GameType
from ..ui import (
    game_keyboard,
    menu_keyboard,
)
from .shared import (
    GamePresenter,
    final_choice_label,
    game_error_text,
    game_names,
    notify_turn,
    safe_send,
    telegram_user,
    word_round_result_text,
)


def register_handlers(
    router: Router,
    service: GameService,
) -> None:
    presenter = GamePresenter(service)
    send_game_view = presenter.send_game_view

    @router.message(F.text | F.caption)
    async def final_text_message(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        text = (message.text or message.caption or "").strip()
        if not text or text.startswith("/"):
            return

        user_id = message.from_user.id
        await service.save_user(telegram_user(message.from_user))

        destinations: list[tuple[str, Game]] = []
        for game in await service.active_games(user_id):
            if (
                game.status is GameStatus.FINISHED
                and game.loser_id == user_id
                and game.final_choice is not None
                and game.final_prompt_text is not None
                and game.final_response_text is None
            ):
                destinations.append(("final_response", game))
            elif (
                game.game_type is GameType.TRUTH_OR_DARE
                and game.status is GameStatus.ACTIVE
                and game.phase is GamePhase.CHOICE
                and game.challenge_asker_id == user_id
                and game.challenge_kind is not None
                and game.challenge_prompt_text is None
            ):
                destinations.append(("challenge_prompt", game))
            elif (
                game.game_type is GameType.TRUTH_OR_DARE
                and game.status is GameStatus.ACTIVE
                and game.phase is GamePhase.GUESSING
                and game.challenge_respondent_id == user_id
                and game.challenge_prompt_text is not None
                and game.challenge_response_text is None
            ):
                destinations.append(("challenge_response", game))
            elif (
                game.status is GameStatus.FINISHED
                and game.winner_id == user_id
                and game.final_choice is not None
                and game.final_prompt_text is None
            ):
                destinations.append(("final_prompt", game))
            elif (
                game.game_type is GameType.WORD_GUESS
                and game.status is GameStatus.ACTIVE
                and game.phase is GamePhase.HIDING
                and game.hider_id == user_id
                and game.word_secret is None
            ):
                destinations.append(("word_secret", game))
            elif (
                game.game_type is GameType.WORD_GUESS
                and game.status is GameStatus.ACTIVE
                and game.phase is GamePhase.GUESSING
                and game.guesser_id == user_id
                and game.word_secret is not None
            ):
                destinations.append(("word_guess", game))

        destination: tuple[str, Game] | None = destinations[0] if len(destinations) == 1 else None
        if len(destinations) > 1:
            replied_message_id = getattr(
                getattr(message, "reply_to_message", None), "message_id", None
            )
            chat_id = message.chat.id
            for candidate in destinations:
                stored = await service.game_message(candidate[1].id, user_id)
                if stored == (chat_id, replied_message_id):
                    destination = candidate
                    break
            if destination is None:
                await message.answer(
                    "⚠️ <b>چند بازی منتظر متن تو هستند.</b>\n\n"
                    "از «ادامهٔ بازی» کارت بازی موردنظر را باز کن و پاسخت را با Reply "
                    "به همان کارت بفرست تا زیر بازی اشتباه ذخیره نشود.",
                    reply_markup=menu_keyboard(has_active_games=True),
                )
                return

        if destination is None:
            return

        destination_kind, destination_game = destination
        if destination_kind == "challenge_prompt":
            try:
                game = await service.submit_challenge_prompt(destination_game.id, user_id, text)
            except GameError as error:
                await message.answer(game_error_text(error))
                return
            await message.answer(
                "✅ <b>سؤال یا چالش ارسال شد.</b> منتظر پاسخ هم‌بازی‌ات باش.",
                reply_markup=menu_keyboard(has_active_games=True),
            )
            await send_game_view(bot, game, user_id)
            if game.challenge_respondent_id is not None:
                await send_game_view(bot, game, game.challenge_respondent_id)
                await notify_turn(
                    bot,
                    game.challenge_respondent_id,
                    "🔔 نوبت توست به سؤال یا چالش پاسخ بدهی!",
                )
            return

        if destination_kind == "word_secret":
            try:
                game = await service.choose_word(destination_game.id, user_id, text)
            except GameError as error:
                await message.answer(game_error_text(error))
                return
            word_length = len(game.word_secret or "")
            await message.answer(
                f"✅ <b>کلمهٔ {word_length} حرفی ثبت شد.</b>\n"
                f"هم‌بازی‌ات {word_length} فرصت برای حدس دارد.",
                reply_markup=menu_keyboard(has_active_games=True),
            )
            await send_game_view(bot, game, user_id)
            if game.guesser_id is not None:
                await send_game_view(bot, game, game.guesser_id)
                await notify_turn(
                    bot,
                    game.guesser_id,
                    f"🔔 کلمه آماده شد: {word_length} حرف و {word_length} فرصت حدس داری!",
                )
            return

        if destination_kind == "word_guess":
            try:
                result = await service.guess_word(destination_game.id, user_id, text)
            except GameError as error:
                await message.answer(game_error_text(error))
                return
            names = await game_names(service, result.game)
            prefix = word_round_result_text(result, names)
            if result.round_finished:
                await message.answer(
                    "✅ <b>دور تمام شد.</b>",
                    reply_markup=menu_keyboard(has_active_games=True),
                )
            else:
                remaining = len(result.secret) - result.game.word_attempts
                await message.answer(
                    f"✅ حدست بررسی شد؛ <b>{remaining}</b> فرصت دیگر داری.",
                    reply_markup=menu_keyboard(has_active_games=True),
                )
            await send_game_view(bot, result.game, user_id, prefix)
            opponent_id = result.game.opponent_of(user_id)
            if opponent_id is not None:
                await send_game_view(bot, result.game, opponent_id, prefix)
            if result.match_finished and result.game.loser_id is not None:
                await notify_turn(
                    bot,
                    result.game.loser_id,
                    "🔔 بازی تمام شد؛ نوبت توست جرئت یا حقیقت را انتخاب کنی!",
                )
            elif result.round_finished and result.game.hider_id is not None:
                await notify_turn(
                    bot,
                    result.game.hider_id,
                    "🔔 دور تازه شروع شد؛ نوبت توست کلمهٔ مخفی را انتخاب کنی!",
                )
            return

        if destination_kind == "final_response":
            try:
                game = await service.submit_final_response(destination_game.id, user_id, text)
            except GameError as error:
                await message.answer(game_error_text(error))
                return
            names = await game_names(service, game)
            loser = escape(names.get(user_id, message.from_user.full_name))
            label = final_choice_label(game)
            await message.answer(
                "✅ <b>پاسخت ارسال شد.</b>\n\n"
                "حالا منتظر تأیید هم‌بازی‌ات باش؛ تأیید یعنی یک امتیاز برای تو.",
                reply_markup=menu_keyboard(has_active_games=True),
            )
            if game.winner_id is not None:
                await safe_send(
                    bot,
                    game.winner_id,
                    f"📩 <b>پاسخ {loser} برای {label}</b>\n\n{escape(text)}\n\n"
                    "آیا درست انجام شده؟ تأیید تو ۱ امتیاز به او می‌دهد.",
                    game_keyboard(game, game.winner_id),
                )
            return

        if destination_kind == "challenge_response":
            try:
                game = await service.submit_challenge_response(destination_game.id, user_id, text)
            except GameError as error:
                await message.answer(game_error_text(error))
                return
            await message.answer(
                "✅ <b>پاسخت ارسال شد.</b> منتظر تأیید هم‌بازی‌ات باش.",
                reply_markup=menu_keyboard(has_active_games=True),
            )
            if game.challenge_asker_id is not None:
                await safe_send(
                    bot,
                    game.challenge_asker_id,
                    f"📩 <b>پاسخ هم‌بازی‌ات:</b>\n\n{escape(text)}\n\nآیا درست انجام شده؟",
                    game_keyboard(game, game.challenge_asker_id),
                )
            return

        if destination_kind == "final_prompt":
            try:
                game = await service.submit_final_prompt(destination_game.id, user_id, text)
            except GameError as error:
                await message.answer(game_error_text(error))
                return
            names = await game_names(service, game)
            winner = escape(names.get(user_id, message.from_user.full_name))
            label = final_choice_label(game)
            await message.answer(
                "✅ <b>سؤال یا چالش ارسال شد.</b>\n\nوقتی جواب بدهد، برایت می‌فرستم.",
                reply_markup=menu_keyboard(has_active_games=True),
            )
            if game.loser_id is not None:
                await safe_send(
                    bot,
                    game.loser_id,
                    f"📨 <b>{winner} برای {label} این پیام را فرستاد:</b>\n\n"
                    f"{escape(game.final_prompt_text or '')}\n\n"
                    "جوابت را همینجا برای ربات بفرست تا برای برنده ارسال شود.",
                )
            return
