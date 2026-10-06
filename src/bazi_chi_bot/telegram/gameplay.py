"""Aiogram handlers for private-chat setup, invitations, and gameplay."""

from __future__ import annotations

from datetime import date
from html import escape
from secrets import choice

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from ..game import (
    DailyChallengeClosed,
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
from .keyboards import final_challenge_keyboard
from .shared import (
    GamePresenter,
    final_choice_label,
    game_error_text,
    game_names,
    mastermind_round_result_text,
    round_result_text,
    safe_edit,
    send_full_game_text_if_needed,
    telegram_user,
)

GAME_SETUP: dict[GameType, tuple[str, str, int]] = {
    GameType.GOL_YA_POOCH: (
        "گل یا پوچ",
        "✊ <b>چند مشت داشته باشیم؟</b>\n\nاز ۲ تا ۶ مشت انتخاب کن.",
        2,
    ),
    GameType.TIC_TAC_TOE: (
        "دوز سه‌تایی",
        (
            "❌⭕ <b>دوز سه‌تایی</b>\n\nچند دست بازی کنیم؟\n"
            "دست مساوی امتیاز ندارد و دوباره بازی می‌شود."
        ),
        2,
    ),
    GameType.ROCK_PAPER_SCISSORS: (
        "سنگ، کاغذ، قیچی",
        (
            "✊✋✌️ <b>سنگ، کاغذ، قیچی</b>\n\nچند دست بازی کنیم؟ "
            "انتخاب نفر اول تا حرکت نفر دوم پنهان می‌ماند. مساوی هم یک دست حساب می‌شود."
        ),
        2,
    ),
    GameType.TRUTH_OR_DARE: (
        "جرئت یا حقیقت",
        (
            "🎭 <b>جرئت یا حقیقت</b>\n\n"
            "در هر دور یک نفر انتخاب می‌کند و طرف مقابل پاسخ می‌دهد؛ "
            "پاسخ تأییدشده ۱ امتیاز دارد.\n\nچند دور بازی کنیم؟"
        ),
        2,
    ),
    GameType.WORD_GUESS: (
        "حدس کلمه",
        (
            "🔤 <b>حدس کلمه</b>\n\n"
            "یک نفر کلمه را انتخاب می‌کند و نفر مقابل به تعداد حروف آن فرصت حدس دارد. "
            "نقش‌ها بعد از هر دور عوض می‌شوند.\n\nچند دور بازی کنیم؟"
        ),
        2,
    ),
    GameType.MASTERMIND: (
        "فکر بکر",
        (
            "🎨 <b>فکر بکر</b>\n\n"
            "کد مخفی چهاررنگ را با آبی، زرد، مشکی، سفید، قرمز و سبز بساز؛ "
            "مهره‌های سیاه و سفید راهنمایی‌ات می‌کنند.\n"
            "در هر دور دقیقاً ۸ فرصت حدس داری. چند دور بازی کنیم؟"
        ),
        8,
    ),
}

FINAL_CHALLENGE_TYPES = frozenset(GAME_SETUP) - {GameType.TRUTH_OR_DARE}


def register_handlers(
    router: Router,
    service: GameService,
    *,
    admin_ids: frozenset[int] = frozenset(),
) -> None:
    presenter = GamePresenter(service)
    send_game_view = presenter.send_game_view
    edit_game_view = presenter.edit_game_view

    async def after_human_turn(game, prefix: str | None):
        if not game.is_solo:
            return game, prefix
        advance = await service.advance_bot(game.id)
        if advance.messages:
            bot_text = "\n".join(advance.messages[-8:])
            prefix = f"{prefix}\n\n{bot_text}" if prefix else bot_text
        return advance.game, prefix

    async def show_setup(
        callback: CallbackQuery, game_type: GameType, *, random: bool = False,
        solo: bool = False, final_challenge_enabled: bool | None = None,
    ) -> None:
        name, text, fists = GAME_SETUP[game_type]
        if solo and game_type is GameType.TIC_TAC_TOE:
            text = (
                "❌⭕ <b>دوز با ربات</b>\n\nچند دست بازی کنیم؟\n"
                "در حالت تک‌نفره دست مساوی هم جزو دست‌ها حساب می‌شود."
            )
        if random:
            text = f"🎲 <b>بازی شانسی: {name}</b>\n\n{text}"
        if final_challenge_enabled is not None:
            state = "دارد ✅" if final_challenge_enabled else "ندارد ❌"
            text += f"\n\n🔥 چالش پایانی: <b>{state}</b>"
        keyboard = (
            fists_keyboard(solo=solo, final_challenge_enabled=final_challenge_enabled)
            if game_type is GameType.GOL_YA_POOCH
            else hands_keyboard(
                fists, game_type, solo=solo,
                final_challenge_enabled=final_challenge_enabled,
            )
        )
        await safe_edit(callback, text, keyboard)
        await callback.answer()

    async def ask_final_challenge(
        callback: CallbackQuery, game_type: GameType, *, random: bool = False
    ) -> None:
        name = GAME_SETUP[game_type][0]
        prefix = "🎲 بازی شانسی: " if random else "🎮 بازی: "
        await safe_edit(
            callback,
            f"{prefix}<b>{name}</b>\n\n🔥 می‌خواهی بازنده در پایان بازی «جرئت یا حقیقت» انجام بدهد؟",
            final_challenge_keyboard(game_type, random=random),
        )
        await callback.answer()

    def setup_final_enabled(data: str, expected_parts: int) -> bool:
        parts = data.split(":")
        if len(parts) == expected_parts:
            return True  # Buttons sent before this change keep their original behavior.
        if len(parts) == expected_parts + 1 and parts[-1] in ("0", "1"):
            return parts[-1] == "1"
        raise ValueError("Invalid final challenge choice")

    def setup_rounds(data: str, route: str) -> tuple[int, bool]:
        parts = data.split(":")
        if parts[:2] != ["setup", route]:
            raise ValueError("Invalid game route")
        enabled = setup_final_enabled(data, 3)
        return int(parts[2]), enabled

    @router.callback_query(F.data == "menu:new")
    async def new_game(callback: CallbackQuery) -> None:
        await service.save_user(telegram_user(callback.from_user))
        user = await service.get_user(callback.from_user.id)
        if user is not None and not user.is_activated and callback.from_user.id not in admin_ids:
            await safe_edit(
                callback,
                "🤖 <b>بازی‌های رایگان با ربات</b>\n\n"
                "یکی از پنج بازی را انتخاب کن. جرئت یا حقیقت در بازی دونفره و گروهی است. "
                "برای بازی دونفره، تورنومنت و گروه می‌توانی اشتراک ویژه بگیری.",
                game_types_keyboard(solo=True),
            )
            await callback.answer()
            return
        await safe_edit(
            callback,
            "🎮 <b>کدام بازی را شروع می‌کنیم؟</b>\n\n"
            "یکی از بازی‌ها را انتخاب کن یا بگذار ربات با «بازی شانسی» انتخاب کند.",
            game_types_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "setup:type:random")
    async def setup_random(callback: CallbackQuery) -> None:
        game_type = choice(tuple(GAME_SETUP))
        if game_type in FINAL_CHALLENGE_TYPES:
            await ask_final_challenge(callback, game_type, random=True)
        else:
            await show_setup(callback, game_type, random=True)

    @router.callback_query(F.data.startswith("setup:final:"))
    async def setup_final_challenge(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        try:
            if len(parts) not in (4, 5) or parts[3] not in ("0", "1"):
                raise ValueError
            if len(parts) == 5 and parts[4] != "random":
                raise ValueError
            game_type = GameType(parts[2])
            if game_type not in FINAL_CHALLENGE_TYPES:
                raise ValueError
        except ValueError:
            await callback.answer("انتخاب نامعتبر است.", show_alert=True)
            return
        await show_setup(
            callback, game_type, random=len(parts) == 5,
            final_challenge_enabled=parts[3] == "1",
        )

    @router.callback_query(F.data == "setup:solo")
    async def setup_solo(callback: CallbackQuery) -> None:
        await safe_edit(
            callback,
            "🤖 <b>بازی تک‌نفره با ربات</b>\n\n"
            "یکی از پنج بازی را انتخاب کن. جرئت یا حقیقت در این حالت اجرا نمی‌شود. "
            "اگر ربات را شکست بدهی، یک امتیاز می‌گیری.",
            game_types_keyboard(solo=True),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("setup:solo:type:"))
    async def setup_solo_type(callback: CallbackQuery) -> None:
        key = (callback.data or "").rsplit(":", 1)[-1]
        types = {
            "gol": GameType.GOL_YA_POOCH,
            "ttt": GameType.TIC_TAC_TOE,
            "rps": GameType.ROCK_PAPER_SCISSORS,
            "word": GameType.WORD_GUESS,
            "mastermind": GameType.MASTERMIND,
        }
        if key == "random":
            await show_setup(callback, choice(tuple(types.values())), random=True, solo=True)
        elif key in types:
            await show_setup(callback, types[key], solo=True)
        else:
            await callback.answer("بازی نامعتبر است.", show_alert=True)

    @router.callback_query(F.data.startswith("setup:solo:f:"))
    async def setup_solo_fists(callback: CallbackQuery) -> None:
        try:
            fists = int((callback.data or "").rsplit(":", 1)[-1])
            if fists not in range(2, 7):
                raise ValueError
        except ValueError:
            await callback.answer("تعداد مشت معتبر نیست.", show_alert=True)
            return
        await safe_edit(
            callback, f"🤖 {fists} مشت انتخاب شد. چند دست با ربات بازی کنیم؟",
            hands_keyboard(fists, solo=True),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("setup:solo:play:"))
    async def start_solo(callback: CallbackQuery, bot: Bot) -> None:
        try:
            _, _, _, type_text, fists_text, hands_text = (callback.data or "").split(":")
            await service.save_user(telegram_user(callback.from_user))
            advance = await service.create_solo_game(
                callback.from_user.id, int(fists_text), int(hands_text), GameType(type_text)
            )
        except (ValueError, GameError):
            await callback.answer("تنظیمات بازی معتبر نیست.", show_alert=True)
            return
        prefix = "🤖 <b>بازی با ربات شروع شد!</b>"
        if advance.messages:
            prefix += "\n" + "\n".join(advance.messages[-4:])
        await edit_game_view(callback, bot, advance.game, prefix, fresh=True)
        await callback.answer("بازی شروع شد ✅")

    @router.callback_query(F.data == "setup:type:ttt")
    async def setup_tic_tac_toe(callback: CallbackQuery) -> None:
        await ask_final_challenge(callback, GameType.TIC_TAC_TOE)

    @router.callback_query(F.data == "setup:type:rps")
    async def setup_rps(callback: CallbackQuery) -> None:
        await ask_final_challenge(callback, GameType.ROCK_PAPER_SCISSORS)

    @router.callback_query(F.data.startswith("setup:rps:"))
    async def create_rps(callback: CallbackQuery, bot: Bot) -> None:
        try:
            hands, enabled = setup_rounds(callback.data or "", "rps")
            await service.save_user(telegram_user(callback.from_user))
            game = await service.create_game(
                callback.from_user.id, 2, hands, GameType.ROCK_PAPER_SCISSORS,
                final_challenge_enabled=enabled,
            )
        except (ValueError, GameError):
            await callback.answer("تنظیمات بازی درست نیست.", show_alert=True)
            return
        await edit_game_view(callback, bot, game, "✅ <b>بازی آماده شد!</b>")
        await callback.answer("لینک دعوت آماده‌ست 📨")

    @router.callback_query(F.data == "setup:type:tod")
    async def setup_truth_or_dare(callback: CallbackQuery) -> None:
        await show_setup(callback, GameType.TRUTH_OR_DARE)

    @router.callback_query(F.data.startswith("setup:tod:"))
    async def create_truth_or_dare(callback: CallbackQuery, bot: Bot) -> None:
        try:
            hands, _ = setup_rounds(callback.data or "", "tod")
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
        await ask_final_challenge(callback, GameType.WORD_GUESS)

    @router.callback_query(F.data == "setup:type:mastermind")
    async def setup_mastermind(callback: CallbackQuery) -> None:
        await ask_final_challenge(callback, GameType.MASTERMIND)

    @router.callback_query(F.data.startswith("setup:mastermind:"))
    async def create_mastermind(callback: CallbackQuery, bot: Bot) -> None:
        try:
            rounds, enabled = setup_rounds(callback.data or "", "mastermind")
            await service.save_user(telegram_user(callback.from_user))
            game = await service.create_game(
                callback.from_user.id, 8, rounds, GameType.MASTERMIND,
                final_challenge_enabled=enabled,
            )
        except ValueError, GameError:
            await callback.answer("تنظیمات بازی درست نیست.", show_alert=True)
            return
        await edit_game_view(callback, bot, game, "✅ <b>بازی آماده شد!</b>")
        await callback.answer("لینک دعوت آماده‌ست 📨")

    @router.callback_query(F.data.startswith("setup:word:"))
    async def create_word_guess(callback: CallbackQuery, bot: Bot) -> None:
        try:
            rounds, enabled = setup_rounds(callback.data or "", "word")
            await service.save_user(telegram_user(callback.from_user))
            game = await service.create_game(
                callback.from_user.id, 2, rounds, GameType.WORD_GUESS,
                final_challenge_enabled=enabled,
            )
        except ValueError, GameError:
            await callback.answer("تنظیمات بازی درست نیست.", show_alert=True)
            return
        await edit_game_view(callback, bot, game, "✅ <b>بازی آماده شد!</b>")
        await callback.answer("لینک دعوت آماده‌ست 📨")

    @router.callback_query(F.data.startswith("setup:ttt:"))
    async def create_tic_tac_toe(callback: CallbackQuery, bot: Bot) -> None:
        try:
            hands, enabled = setup_rounds(callback.data or "", "ttt")
            await service.save_user(telegram_user(callback.from_user))
            game = await service.create_game(
                callback.from_user.id, 2, hands, GameType.TIC_TAC_TOE,
                final_challenge_enabled=enabled,
            )
        except ValueError, GameError:
            await callback.answer("تنظیمات بازی درست نیست.", show_alert=True)
            return
        await edit_game_view(callback, bot, game, "✅ <b>بازی آماده شد!</b>")
        await callback.answer("لینک دعوت آماده‌ست 📨")

    @router.callback_query(F.data == "setup:type:gol")
    async def setup_gol(callback: CallbackQuery) -> None:
        await ask_final_challenge(callback, GameType.GOL_YA_POOCH)

    @router.callback_query(F.data.startswith("setup:f:"))
    async def choose_fists(callback: CallbackQuery) -> None:
        try:
            parts = (callback.data or "").split(":")
            enabled = setup_final_enabled(callback.data or "", 3)
            fists = int(parts[2])
        except ValueError, IndexError:
            await callback.answer("انتخاب نامعتبر است.", show_alert=True)
            return
        if fists not in range(2, 7):
            await callback.answer("تعداد مشت باید بین ۲ تا ۶ باشد.", show_alert=True)
            return
        await safe_edit(
            callback,
            f"✊ <b>{fists} مشت</b> انتخاب شد.\n\n🧮 چند دست بازی کنیم؟",
            hands_keyboard(fists, final_challenge_enabled=enabled),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("setup:h:"))
    async def choose_hands(callback: CallbackQuery, bot: Bot) -> None:
        try:
            data = callback.data or ""
            enabled = setup_final_enabled(data, 4)
            _, _, fists_text, hands_text = data.split(":")[:4]
            fists, hands = int(fists_text), int(hands_text)
            game = await service.create_game(
                callback.from_user.id, fists, hands,
                final_challenge_enabled=enabled,
            )
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
                game, prefix = await after_human_turn(result.game, prefix)
                await edit_game_view(callback, bot, game, prefix, fresh=True)
                opponent_id = game.opponent_of(callback.from_user.id)
                if opponent_id is not None and not game.is_solo:
                    await send_game_view(bot, result.game, opponent_id, prefix, fresh=True)
            elif action == "rps":
                result = await service.play_rps(
                    game_id, callback.from_user.id, value, version
                )
                if result.round_finished:
                    labels = {"rock": "✊ سنگ", "paper": "✋ کاغذ", "scissors": "✌️ قیچی"}
                    names = await game_names(service, result.game)
                    outcome = (
                        "🤝 این دست مساوی شد؛ امتیازی ندارد."
                        if result.point_winner_id is None
                        else f"⭐ امتیاز این دست برای <b>{escape(names[result.point_winner_id])}</b>"
                    )
                    prefix = (
                        f"🎲 حرکت‌ها: {escape(names[result.game.creator_id])}: "
                        f"<b>{labels[result.creator_move]}</b> | "
                        f"{escape(names[result.game.player2_id])}: "
                        f"<b>{labels[result.player2_move]}</b>\n{outcome}"
                    )
                else:
                    prefix = "✅ حرکتت ثبت شد؛ منتظر انتخاب هم‌بازی‌ات باش."
                game, prefix = await after_human_turn(result.game, prefix)
                await edit_game_view(callback, bot, game, prefix, fresh=True)
                if not game.is_solo:
                    opponent_id = game.opponent_of(callback.from_user.id)
                    if opponent_id is not None:
                        await send_game_view(
                            bot, result.game, opponent_id,
                            prefix if result.round_finished else "🎲 نوبت توست؛ حرکتت را انتخاب کن.",
                            fresh=True,
                        )
            elif action == "hide":
                game = await service.hide_fist(game_id, callback.from_user.id, int(value), version)
                game, prefix = await after_human_turn(game, "✅ انتخابت ثبت شد.")
                await edit_game_view(callback, bot, game, prefix, fresh=True)
                if game.guesser_id is not None and not game.is_solo:
                    await send_game_view(
                        bot, game, game.guesser_id, "🌸 گل پنهان شد؛ حالا نوبت حدس توست!",
                        fresh=True,
                    )
            elif action == "guess":
                result = await service.guess_fist(
                    game_id, callback.from_user.id, int(value), version
                )
                names = await game_names(service, result.game)
                prefix = round_result_text(result, names)
                game, prefix = await after_human_turn(result.game, prefix)
                await edit_game_view(callback, bot, game, prefix, fresh=True)
                opponent_id = game.opponent_of(callback.from_user.id)
                if opponent_id is not None and not game.is_solo:
                    await send_game_view(bot, result.game, opponent_id, prefix, fresh=True)
            elif action == "mastermind":
                if value == "reset":
                    game = await service.reset_mastermind_selection(
                        game_id, callback.from_user.id, version
                    )
                    await edit_game_view(callback, bot, game, "انتخاب‌ها پاک شد.")
                else:
                    selection = await service.select_mastermind_color(
                        game_id, callback.from_user.id, value, version
                    )
                    game = selection.game
                    if not selection.complete:
                        await edit_game_view(callback, bot, game)
                    elif selection.result is None:
                        game, prefix = await after_human_turn(game, "✅ کد مخفی ثبت شد.")
                        await edit_game_view(callback, bot, game, prefix, fresh=True)
                        if game.guesser_id is not None and not game.is_solo:
                            await send_game_view(
                                bot, game, game.guesser_id,
                                "🎨 کد آماده شد؛ حالا رنگ‌ها را حدس بزن!",
                                fresh=True,
                            )
                    else:
                        result = selection.result
                        names = await game_names(service, result.game)
                        prefix = mastermind_round_result_text(result, names)
                        game, prefix = await after_human_turn(result.game, prefix)
                        await edit_game_view(callback, bot, game, prefix, fresh=True)
                        opponent_id = game.opponent_of(callback.from_user.id)
                        if opponent_id is not None and not game.is_solo:
                            await send_game_view(bot, result.game, opponent_id, prefix, fresh=True)
            elif action == "final":
                game = await service.choose_final(game_id, callback.from_user.id, value, version)
                label = final_choice_label(game)
                await send_full_game_text_if_needed(
                    bot, callback.from_user.id, game, "سؤال یا چالش", game.final_prompt_text
                )
                if game.winner_id is not None:
                    await send_full_game_text_if_needed(
                        bot, game.winner_id, game, "سؤال یا چالش", game.final_prompt_text
                    )
                await edit_game_view(
                    callback,
                    bot,
                    game,
                    f"✅ انتخاب نهایی ثبت شد: <b>{label}</b>",
                    fresh=True,
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
                        fresh=True,
                    )
            elif action == "challenge":
                game = await service.choose_challenge(
                    game_id, callback.from_user.id, value, version
                )
                if game.challenge_prompt_text is not None:
                    await send_full_game_text_if_needed(
                        bot, callback.from_user.id, game,
                        "سؤال یا چالش", game.challenge_prompt_text,
                    )
                    if game.challenge_respondent_id is not None:
                        await send_full_game_text_if_needed(
                            bot, game.challenge_respondent_id, game,
                            "سؤال یا چالش", game.challenge_prompt_text,
                        )
                if game.challenge_prompt_text is None:
                    await edit_game_view(
                        callback,
                        bot,
                        game,
                        "✅ انتخاب ثبت شد؛ سؤال تازه‌ای در بانک نمانده است. متن خودت را بفرست.",
                        fresh=True,
                    )
                    if game.challenge_respondent_id is not None:
                        await send_game_view(bot, game, game.challenge_respondent_id, fresh=True)
                else:
                    await edit_game_view(
                        callback, bot, game, "✅ سؤال یا چالش انتخاب شد.", fresh=True
                    )
                    await send_game_view(
                        bot,
                        game,
                        game.challenge_respondent_id,
                        "🎭 نوبت توست؛ سؤال یا چالش را پاسخ بده.",
                        fresh=True,
                    )
            elif action == "challenge_review":
                if value not in ("yes", "no"):
                    raise InvalidFinalChoice
                previous = await service.get_game(game_id)
                game = await service.review_challenge(
                    game_id, callback.from_user.id, value == "yes", version
                )
                result_text = (
                    "✅ <b>پاسخ تأیید شد؛ پاسخ‌دهنده ۱ امتیاز گرفت.</b>"
                    if value == "yes"
                    else "❌ <b>پاسخ تأیید نشد؛ امتیازی ثبت نشد.</b>"
                )
                if game.status is GameStatus.ACTIVE:
                    result_text += "\n🎲 دور تازه شروع شد."
                await edit_game_view(callback, bot, game, result_text, fresh=True)
                if previous.challenge_respondent_id is not None:
                    await send_game_view(
                        bot, game, previous.challenge_respondent_id, result_text, fresh=True
                    )
            elif action == "review":
                if value not in ("yes", "no"):
                    raise InvalidFinalChoice
                game = await service.review_final_response(
                    game_id, callback.from_user.id, value == "yes", version
                )
                result_text = (
                    "✅ <b>چالش پایانی تأیید شد؛ پاسخ‌دهنده ۱ امتیاز گرفت.</b>"
                    if value == "yes"
                    else "❌ <b>چالش پایانی تأیید نشد؛ امتیازی اضافه نشد.</b>"
                )
                await edit_game_view(callback, bot, game, result_text, fresh=True)
                if game.loser_id is not None:
                    await send_game_view(bot, game, game.loser_id, result_text, fresh=True)
            elif action == "cancel":
                game = await service.cancel_waiting(game_id, callback.from_user.id, version)
                await edit_game_view(callback, bot, game)
            else:
                await callback.answer("عملیات ناشناخته است.", show_alert=True)
                return
        except (ValueError, GameError) as error:
            game_error = error if isinstance(error, GameError) else InvalidFist()
            await callback.answer(game_error_text(game_error), show_alert=True)
            if isinstance(game_error, DailyChallengeClosed):
                try:
                    current = await service.get_game(game_id)
                    if current.daily_challenge_date and current.has_player(callback.from_user.id):
                        await service.daily.close_day(date.fromisoformat(current.daily_challenge_date))
                        current = await service.get_game(game_id)
                        await edit_game_view(
                            callback, bot, current, "🏁 زمان چالش امروز تمام شد.", fresh=True
                        )
                except GameError:
                    pass
            if isinstance(game_error, StaleAction):
                try:
                    current = await service.get_game(game_id)
                    if current.has_player(callback.from_user.id):
                        await edit_game_view(callback, bot, current)
                except GameError:
                    pass
            return
        await callback.answer("انجام شد ✅")
