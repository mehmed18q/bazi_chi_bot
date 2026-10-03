"""Aiogram handlers for private-chat setup, invitations, and gameplay."""

from __future__ import annotations

from datetime import date
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import Message

from ..game import (
    DailyChallengeClosed,
    GameError,
    GameService,
)
from ..models import Game, GamePhase, GameStatus, GameType
from ..ui import (
    menu_keyboard,
)
from .shared import (
    GamePresenter,
    final_choice_label,
    game_error_text,
    game_names,
    send_full_game_text_if_needed,
    telegram_user,
    word_round_result_text,
)


def register_handlers(
    router: Router,
    service: GameService,
    *,
    admin_ids: frozenset[int] = frozenset(),
) -> None:
    presenter = GamePresenter(service)
    send_game_view = presenter.send_game_view

    async def show_closed_daily_game(game: Game, user_id: int, bot: Bot) -> None:
        if game.daily_challenge_date is None:
            return
        await service.daily.close_day(date.fromisoformat(game.daily_challenge_date))
        await send_game_view(
            bot, await service.get_game(game.id), user_id,
            "🏁 زمان چالش امروز تمام شد.", fresh=True,
        )

    @router.message(F.text | F.caption)
    async def final_text_message(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        text = (message.text or message.caption or "").strip()
        if not text or text.startswith("/"):
            return

        user_id = message.from_user.id
        await service.save_user(telegram_user(message.from_user))
        user = await service.get_user(user_id)
        premium = user_id in admin_ids or bool(user and user.is_activated)

        destinations: list[tuple[str, Game]] = []
        games = (
            await service.active_games(user_id)
            if premium else await service.queries.active_games(user_id)
        )
        for game in games:
            if not premium and (not game.is_solo or game.tournament_id is not None):
                continue
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
            await send_game_view(bot, game, user_id, fresh=True)
            if game.challenge_respondent_id is not None:
                await send_full_game_text_if_needed(
                    bot, game.challenge_respondent_id, game,
                    "سؤال یا چالش", game.challenge_prompt_text,
                )
                await send_game_view(
                    bot, game, game.challenge_respondent_id,
                    "🎭 نوبت توست به سؤال یا چالش پاسخ بدهی.", fresh=True,
                )
            return

        if destination_kind == "word_secret":
            try:
                game = await service.choose_word(destination_game.id, user_id, text)
            except GameError as error:
                await message.answer(game_error_text(error))
                if isinstance(error, DailyChallengeClosed):
                    await show_closed_daily_game(destination_game, user_id, bot)
                return
            word_length = len(game.word_secret or "")
            if game.is_solo:
                advance = await service.advance_bot(game.id)
                game = advance.game
            await message.answer(
                f"✅ <b>کلمهٔ {word_length} حرفی ثبت شد.</b>\n"
                + (
                    "\n".join(advance.messages[-8:])
                    if game.is_solo else f"هم‌بازی‌ات {word_length} فرصت برای حدس دارد."
                ),
                reply_markup=menu_keyboard(has_active_games=True),
            )
            await send_game_view(bot, game, user_id, fresh=True)
            if game.guesser_id is not None and not game.is_solo:
                await send_game_view(
                    bot, game, game.guesser_id,
                    f"🔔 کلمه آماده شد: {word_length} حرف و {word_length} فرصت حدس داری!",
                    fresh=True,
                )
            return

        if destination_kind == "word_guess":
            try:
                result = await service.guess_word(destination_game.id, user_id, text)
            except GameError as error:
                await message.answer(game_error_text(error))
                if isinstance(error, DailyChallengeClosed):
                    await show_closed_daily_game(destination_game, user_id, bot)
                return
            names = await game_names(service, result.game)
            prefix = word_round_result_text(result, names)
            game = result.game
            if game.is_solo:
                advance = await service.advance_bot(game.id)
                game = advance.game
                if advance.messages:
                    prefix += "\n\n" + "\n".join(advance.messages[-8:])
            if result.round_finished:
                result_line = (
                    "🎯 <b>حدست درست بود!</b>"
                    if result.guessed_correctly
                    else "⌛ <b>حدست درست نبود؛ فرصت‌های این دور تمام شد.</b>"
                )
                await message.answer(
                    f"{result_line}\n✅ <b>دور تمام شد.</b>",
                    reply_markup=menu_keyboard(has_active_games=True),
                )
            else:
                remaining = len(result.secret) - result.game.word_attempts
                await message.answer(
                    f"❌ <b>حدست درست نبود.</b> بررسی شد؛ "
                    f"<b>{remaining}</b> فرصت دیگر داری.",
                    reply_markup=menu_keyboard(has_active_games=True),
                )
            await send_game_view(bot, game, user_id, prefix, fresh=True)
            opponent_id = game.opponent_of(user_id)
            if opponent_id is not None and not game.is_solo:
                await send_game_view(bot, result.game, opponent_id, prefix, fresh=True)
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
                await send_full_game_text_if_needed(
                    bot, game.winner_id, game, "پاسخ", game.final_response_text
                )
                await send_game_view(
                    bot,
                    game,
                    game.winner_id,
                    f"📩 <b>پاسخ {loser} برای {label} رسید.</b>\n"
                    "آیا درست انجام شده؟ تأیید تو ۱ امتیاز به او می‌دهد.",
                    fresh=True,
                )
            await send_game_view(bot, game, user_id, fresh=True)
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
                await send_full_game_text_if_needed(
                    bot, game.challenge_asker_id, game, "پاسخ", game.challenge_response_text
                )
                await send_game_view(
                    bot,
                    game,
                    game.challenge_asker_id,
                    "📩 <b>پاسخ هم‌بازی‌ات رسید.</b> آیا درست انجام شده؟",
                    fresh=True,
                )
            await send_game_view(bot, game, user_id, fresh=True)
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
                await send_full_game_text_if_needed(
                    bot, game.loser_id, game, "سؤال یا چالش", game.final_prompt_text
                )
                await send_game_view(
                    bot,
                    game,
                    game.loser_id,
                    f"📨 <b>{winner} برای {label} سؤال یا چالش فرستاد.</b>\n"
                    "جوابت را همینجا برای ربات بفرست تا برای برنده ارسال شود.",
                    fresh=True,
                )
            await send_game_view(bot, game, user_id, fresh=True)
            return
