"""Aiogram handlers for private-chat setup, invitations, and gameplay."""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from ..game import (
    GameError,
    GameService,
    InvalidFinalChoice,
    InvalidFist,
    StaleAction,
)
from ..models import GameStatus, GameType
from ..ui import (
    board_text,
    fists_keyboard,
    game_types_keyboard,
    hands_keyboard,
)
from .shared import (
    GamePresenter,
    final_choice_label,
    game_error_text,
    game_names,
    notify_turn,
    round_result_text,
    safe_edit,
    telegram_user,
)


def register_handlers(
    router: Router,
    service: GameService,
) -> None:
    presenter = GamePresenter(service)
    send_game_view = presenter.send_game_view
    edit_game_view = presenter.edit_game_view

    @router.callback_query(F.data == "menu:new")
    async def new_game(callback: CallbackQuery) -> None:
        await service.save_user(telegram_user(callback.from_user))
        await safe_edit(
            callback,
            "🎮 <b>کدام بازی را شروع می‌کنیم؟</b>\n\n"
            "🌸 گل یا پوچ، ❌⭕ دوز سه‌تایی، 🎭 جرئت یا حقیقت یا 🔤 حدس کلمه؟",
            game_types_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "setup:type:ttt")
    async def setup_tic_tac_toe(callback: CallbackQuery) -> None:
        await safe_edit(
            callback,
            "❌⭕ <b>دوز سه‌تایی</b>\n\nچند دست بازی کنیم؟\n"
            "دست مساوی امتیاز ندارد و دوباره بازی می‌شود.",
            hands_keyboard(2, GameType.TIC_TAC_TOE),
        )
        await callback.answer()

    @router.callback_query(F.data == "setup:type:tod")
    async def setup_truth_or_dare(callback: CallbackQuery) -> None:
        await safe_edit(
            callback,
            "🎭 <b>جرئت یا حقیقت</b>\n\n"
            "در هر دور یک نفر انتخاب می‌کند و طرف مقابل پاسخ می‌دهد؛ "
            "پاسخ تأییدشده ۱ امتیاز دارد.\n\nچند دور بازی کنیم؟",
            hands_keyboard(2, GameType.TRUTH_OR_DARE),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("setup:tod:"))
    async def create_truth_or_dare(callback: CallbackQuery, bot: Bot) -> None:
        try:
            hands = int((callback.data or "").rsplit(":", 1)[1])
            await service.save_user(telegram_user(callback.from_user))
            game = await service.create_game(
                callback.from_user.id, 2, hands, GameType.TRUTH_OR_DARE
            )
        except ValueError, GameError:
            await callback.answer("تنظیمات بازی درست نیست.", show_alert=True)
            return
        await edit_game_view(callback, bot, game, "✅ <b>بازی آماده شد!</b>")
        await callback.answer("لینک دعوت آماده‌ست 📨")

    @router.callback_query(F.data == "setup:type:word")
    async def setup_word_guess(callback: CallbackQuery) -> None:
        await safe_edit(
            callback,
            "🔤 <b>حدس کلمه</b>\n\n"
            "یک نفر کلمه را انتخاب می‌کند و نفر مقابل به تعداد حروف آن فرصت حدس دارد. "
            "نقش‌ها بعد از هر دور عوض می‌شوند.\n\nچند دور بازی کنیم؟",
            hands_keyboard(2, GameType.WORD_GUESS),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("setup:word:"))
    async def create_word_guess(callback: CallbackQuery, bot: Bot) -> None:
        try:
            rounds = int((callback.data or "").rsplit(":", 1)[1])
            await service.save_user(telegram_user(callback.from_user))
            game = await service.create_game(callback.from_user.id, 2, rounds, GameType.WORD_GUESS)
        except ValueError, GameError:
            await callback.answer("تنظیمات بازی درست نیست.", show_alert=True)
            return
        await edit_game_view(callback, bot, game, "✅ <b>بازی آماده شد!</b>")
        await callback.answer("لینک دعوت آماده‌ست 📨")

    @router.callback_query(F.data.startswith("setup:ttt:"))
    async def create_tic_tac_toe(callback: CallbackQuery, bot: Bot) -> None:
        try:
            hands = int((callback.data or "").rsplit(":", 1)[1])
            await service.save_user(telegram_user(callback.from_user))
            game = await service.create_game(callback.from_user.id, 2, hands, GameType.TIC_TAC_TOE)
        except ValueError, GameError:
            await callback.answer("تنظیمات بازی درست نیست.", show_alert=True)
            return
        await edit_game_view(callback, bot, game, "✅ <b>بازی آماده شد!</b>")
        await callback.answer("لینک دعوت آماده‌ست 📨")

    @router.callback_query(F.data == "setup:type:gol")
    async def setup_gol(callback: CallbackQuery) -> None:
        await safe_edit(
            callback,
            "✊ <b>چند مشت داشته باشیم؟</b>\n\nاز ۲ تا ۶ مشت انتخاب کن.",
            fists_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("setup:f:"))
    async def choose_fists(callback: CallbackQuery) -> None:
        try:
            fists = int((callback.data or "").split(":")[2])
        except ValueError, IndexError:
            await callback.answer("انتخاب نامعتبر است.", show_alert=True)
            return
        if fists not in range(2, 7):
            await callback.answer("تعداد مشت باید بین ۲ تا ۶ باشد.", show_alert=True)
            return
        await safe_edit(
            callback,
            f"✊ <b>{fists} مشت</b> انتخاب شد.\n\n🧮 چند دست بازی کنیم؟",
            hands_keyboard(fists),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("setup:h:"))
    async def choose_hands(callback: CallbackQuery, bot: Bot) -> None:
        try:
            _, _, fists_text, hands_text = (callback.data or "").split(":")
            fists, hands = int(fists_text), int(hands_text)
            game = await service.create_game(callback.from_user.id, fists, hands)
        except ValueError, GameError:
            await callback.answer("تنظیمات بازی معتبر نیست.", show_alert=True)
            return
        await edit_game_view(callback, bot, game, "✅ <b>بازی آماده شد!</b>")
        await callback.answer("لینک دعوت آماده است 📨")

    @router.callback_query(F.data.startswith("game:"))
    async def game_action(callback: CallbackQuery, bot: Bot) -> None:
        data = (callback.data or "").split(":")
        if len(data) != 5:
            await callback.answer("دکمه نامعتبر است.", show_alert=True)
            return
        _, game_id_text, version_text, action, value = data
        try:
            game_id = int(game_id_text)
            version = int(version_text)
            if action == "move":
                result = await service.place_mark(
                    game_id, callback.from_user.id, int(value), version
                )
                prefix = None
                if result.round_finished:
                    names = await game_names(service, result.game)
                    outcome = (
                        f"⭐ امتیاز این دست برای <b>{escape(names[result.point_winner_id])}</b>"
                        if result.point_winner_id is not None
                        else "🤝 مساوی شد؛ این دست بدون امتیاز دوباره بازی می‌شود."
                    )
                    prefix = f"{outcome}\n\n{board_text(result.board)}"
                await edit_game_view(callback, bot, result.game, prefix)
                opponent_id = result.game.opponent_of(callback.from_user.id)
                if opponent_id is not None:
                    await send_game_view(bot, result.game, opponent_id, prefix)
                if (
                    result.game.next_player_id is not None
                    and result.game.next_player_id != callback.from_user.id
                ):
                    await notify_turn(bot, result.game.next_player_id)
                elif (
                    result.game.loser_id is not None
                    and result.game.loser_id != callback.from_user.id
                ):
                    await notify_turn(
                        bot,
                        result.game.loser_id,
                        "🔔 بازی تمام شد؛ نوبت توست جرئت یا حقیقت را انتخاب کنی!",
                    )
            elif action == "hide":
                game = await service.hide_fist(game_id, callback.from_user.id, int(value), version)
                await edit_game_view(callback, bot, game, "✅ انتخابت ثبت شد.")
                if game.guesser_id is not None:
                    await send_game_view(
                        bot, game, game.guesser_id, "🌸 گل پنهان شد؛ حالا نوبت حدس توست!"
                    )
                    if game.guesser_id != callback.from_user.id:
                        await notify_turn(bot, game.guesser_id, "🔔 گل پنهان شد؛ نوبت حدس توست!")
            elif action == "guess":
                result = await service.guess_fist(
                    game_id, callback.from_user.id, int(value), version
                )
                names = await game_names(service, result.game)
                prefix = round_result_text(result, names)
                await edit_game_view(callback, bot, result.game, prefix)
                opponent_id = result.game.opponent_of(callback.from_user.id)
                if opponent_id is not None:
                    await send_game_view(bot, result.game, opponent_id, prefix)
                if (
                    not result.match_finished
                    and result.game.hider_id is not None
                    and result.game.hider_id != callback.from_user.id
                ):
                    await notify_turn(bot, result.game.hider_id, "🔔 نوبت توست گل را پنهان کنی!")
                elif (
                    result.match_finished
                    and result.game.loser_id is not None
                    and result.game.loser_id != callback.from_user.id
                ):
                    await notify_turn(
                        bot,
                        result.game.loser_id,
                        "🔔 بازی تمام شد؛ نوبت توست جرئت یا حقیقت را انتخاب کنی!",
                    )
            elif action == "final":
                game = await service.choose_final(game_id, callback.from_user.id, value, version)
                label = final_choice_label(game)
                await edit_game_view(
                    callback,
                    bot,
                    game,
                    f"✅ انتخاب نهایی ثبت شد: <b>{label}</b>",
                )
                if game.winner_id is not None:
                    names = await game_names(service, game)
                    loser = escape(names.get(callback.from_user.id, callback.from_user.full_name))
                    await send_game_view(
                        bot,
                        game,
                        game.winner_id,
                        f"🎭 <b>{loser}</b> انتخابش را انجام داد: <b>{label}</b>"
                        + (
                            "\nسؤال تازه‌ای در بانک موجود نیست؛ سؤال یا چالش را خودت بنویس."
                            if game.final_question_id is None
                            else ""
                        ),
                    )
                    if game.final_question_id is None:
                        await notify_turn(
                            bot,
                            game.winner_id,
                            "🔔 نوبت توست سؤال یا چالش را بنویسی!",
                        )
            elif action == "challenge":
                game = await service.choose_challenge(
                    game_id, callback.from_user.id, value, version
                )
                if game.challenge_prompt_text is None:
                    await edit_game_view(
                        callback,
                        bot,
                        game,
                        "✅ انتخاب ثبت شد؛ سؤال تازه‌ای در بانک نمانده است. متن خودت را بفرست.",
                    )
                    if game.challenge_respondent_id is not None:
                        await send_game_view(bot, game, game.challenge_respondent_id)
                else:
                    await edit_game_view(callback, bot, game, "✅ سؤال یا چالش انتخاب شد.")
                    await send_game_view(
                        bot,
                        game,
                        game.challenge_respondent_id,
                        "🎭 نوبت توست؛ سؤال یا چالش را پاسخ بده.",
                    )
                    await notify_turn(
                        bot,
                        game.challenge_respondent_id,
                        "🔔 نوبت توست به سؤال یا چالش پاسخ بدهی!",
                    )
            elif action == "challenge_review":
                if value not in ("yes", "no"):
                    raise InvalidFinalChoice
                game = await service.review_challenge(
                    game_id, callback.from_user.id, value == "yes", version
                )
                await edit_game_view(callback, bot, game, "✅ ارزیابی پاسخ ثبت شد.")
                if game.status is GameStatus.ACTIVE and game.challenge_asker_id is not None:
                    await send_game_view(bot, game, game.challenge_asker_id)
                    if game.challenge_asker_id != callback.from_user.id:
                        await notify_turn(
                            bot,
                            game.challenge_asker_id,
                            "🔔 دور تازه شروع شد؛ نوبت توست جرئت یا حقیقت را انتخاب کنی!",
                        )
                elif game.challenge_respondent_id is not None:
                    await send_game_view(bot, game, game.challenge_respondent_id)
            elif action == "review":
                if value not in ("yes", "no"):
                    raise InvalidFinalChoice
                game = await service.review_final_response(
                    game_id, callback.from_user.id, value == "yes", version
                )
                await edit_game_view(callback, bot, game)
                if game.loser_id is not None:
                    await send_game_view(bot, game, game.loser_id)
            elif action == "cancel":
                game = await service.cancel_waiting(game_id, callback.from_user.id, version)
                await edit_game_view(callback, bot, game)
            else:
                await callback.answer("عملیات ناشناخته است.", show_alert=True)
                return
        except (ValueError, GameError) as error:
            game_error = error if isinstance(error, GameError) else InvalidFist()
            await callback.answer(game_error_text(game_error), show_alert=True)
            if isinstance(game_error, StaleAction):
                try:
                    current = await service.get_game(game_id)
                    if current.has_player(callback.from_user.id):
                        await edit_game_view(callback, bot, current)
                except GameError:
                    pass
            return
        await callback.answer("انجام شد ✅")
