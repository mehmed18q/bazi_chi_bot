from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TelegramUser

from bazi_chi_bot.handlers import build_router
from bazi_chi_bot.models import GamePhase, GameType
from bazi_chi_bot.telegram import gameplay
from bazi_chi_bot.telegram.shared import GamePresenter
from bazi_chi_bot.ui import (
    START_TEXT,
    SUPPORT_TEXT,
    TRUTH_OR_DARE_RULE,
    admin_menu_keyboard,
    countdown_users_keyboard,
    game_keyboard,
    game_types_keyboard,
    menu_keyboard,
    render_game,
    stats_text,
)


@pytest.mark.parametrize(
    ("game_type", "name", "next_callback"),
    [
        (GameType.GOL_YA_POOCH, "گل یا پوچ", "setup:f:2"),
        (GameType.TIC_TAC_TOE, "دوز سه‌تایی", "setup:ttt:3"),
        (GameType.ROCK_PAPER_SCISSORS, "سنگ، کاغذ، قیچی", "setup:rps:3"),
        (GameType.TRUTH_OR_DARE, "جرئت یا حقیقت", "setup:tod:3"),
        (GameType.WORD_GUESS, "حدس کلمه", "setup:word:3"),
        (GameType.MASTERMIND, "فکر بکر", "setup:mastermind:3"),
    ],
)
async def test_random_game_opens_the_selected_games_setup(
    service, monkeypatch, game_type, name, next_callback
):
    menu_callbacks = {
        button.callback_data
        for row in game_types_keyboard().inline_keyboard
        for button in row
    }
    assert "setup:type:random" in menu_callbacks

    def pick_game(candidates):
        assert set(candidates) == set(GameType)
        return game_type

    monkeypatch.setattr(gameplay, "choice", pick_game)
    handler = next(
        item.callback
        for item in build_router(service).callback_query.handlers
        if item.callback.__name__ == "setup_random"
    )
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )

    await handler(callback)

    text = callback.message.edit_text.call_args.args[0]
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    callbacks = {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }
    assert "بازی شانسی" in text and name in text
    if game_type is GameType.TRUTH_OR_DARE:
        assert next_callback in callbacks
    else:
        assert {
            f"setup:final:{game_type.value}:0:random",
            f"setup:final:{game_type.value}:1:random",
        } <= callbacks
        final_handler = next(
            item.callback
            for item in build_router(service).callback_query.handlers
            if item.callback.__name__ == "setup_final_challenge"
        )
        callback.data = f"setup:final:{game_type.value}:1:random"
        await final_handler(callback)
        keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
        callbacks = {
            button.callback_data for row in keyboard.inline_keyboard for button in row
        }
        assert f"{next_callback}:1" in callbacks
    assert callback.answer.await_count == (1 if game_type is GameType.TRUTH_OR_DARE else 2)


@pytest.mark.parametrize(
    "game_type,callback_data",
    [
        (GameType.GOL_YA_POOCH, "setup:h:2:3:0"),
        (GameType.TIC_TAC_TOE, "setup:ttt:3:0"),
        (GameType.ROCK_PAPER_SCISSORS, "setup:rps:3:0"),
        (GameType.WORD_GUESS, "setup:word:3:0"),
        (GameType.MASTERMIND, "setup:mastermind:3:0"),
    ],
)
async def test_private_setup_saves_creator_choice(
    service, players, monkeypatch, game_type, callback_data
):
    async def skip_presentation(self, *args, **kwargs):
        return None

    monkeypatch.setattr(GamePresenter, "edit_game_view", skip_presentation)
    handlers = {
        item.callback.__name__: item.callback
        for item in build_router(service).callback_query.handlers
    }
    route = {
        GameType.GOL_YA_POOCH: "choose_hands",
        GameType.TIC_TAC_TOE: "create_tic_tac_toe",
        GameType.ROCK_PAPER_SCISSORS: "create_rps",
        GameType.WORD_GUESS: "create_word_guess",
        GameType.MASTERMIND: "create_mastermind",
    }[game_type]
    first = players[0]
    callback = SimpleNamespace(
        data=callback_data,
        from_user=TelegramUser(id=first.telegram_id, is_bot=False, first_name=first.first_name),
        answer=AsyncMock(),
    )
    await handlers[route](callback, SimpleNamespace())
    game = await service.get_game(1)
    assert game.game_type is game_type
    assert not game.final_challenge_enabled


async def test_only_current_actor_receives_action_keyboard(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 3, 3)
    game = await service.join_game(game.invite_token, second.telegram_id)

    assert game.phase is GamePhase.HIDING
    hider_keyboard = game_keyboard(game, first.telegram_id)
    guesser_keyboard = game_keyboard(game, second.telegram_id)
    assert hider_keyboard is not None
    assert len(hider_keyboard.inline_keyboard[0]) == 3
    assert guesser_keyboard is None

    game = await service.hide_fist(game.id, first.telegram_id, 2, game.version)
    assert game_keyboard(game, first.telegram_id) is None
    assert game_keyboard(game, second.telegram_id) is not None


def test_required_branding_and_final_rule_are_present():
    assert "بازی‌چی" in START_TEXT
    assert "گل یا پوچ" in START_TEXT
    assert "دوز سه‌تایی" in START_TEXT
    assert "جرئت یا حقیقت" in START_TEXT
    assert "حدس کلمه" in START_TEXT
    assert "صادق" in START_TEXT
    assert "@bazi_chi_admin" in SUPPORT_TEXT
    assert "صادق" in SUPPORT_TEXT
    assert "حقیقت" in TRUTH_OR_DARE_RULE
    assert "جرئت" in TRUTH_OR_DARE_RULE
    assert "برنده" in TRUTH_OR_DARE_RULE
    assert "مساوی: <b>1</b>" in stats_text("بازیکن", 3, 1, 1, 0, 0, 0)


async def test_admin_controls_are_behind_an_admin_only_menu(players):
    first, second = players
    regular_callbacks = {
        button.callback_data for row in menu_keyboard().inline_keyboard for button in row
    }
    admin_callbacks = {
        button.callback_data
        for row in menu_keyboard(is_countdown_admin=True).inline_keyboard
        for button in row
    }
    target_callbacks = {
        button.callback_data
        for row in countdown_users_keyboard([first, second]).inline_keyboard
        for button in row
    }

    assert "menu:countdown" not in regular_callbacks
    assert "menu:leaderboard" in regular_callbacks
    assert "menu:leaderboard:all_time" in regular_callbacks
    assert "menu:support" in regular_callbacks
    assert "menu:countdown" not in admin_callbacks
    assert "menu:admin" in admin_callbacks
    admin_menu_callbacks = {
        button.callback_data for row in admin_menu_keyboard().inline_keyboard for button in row
    }
    assert "menu:countdown" in admin_menu_callbacks
    assert f"countdown:user:{first.telegram_id}" in target_callbacks
    assert f"countdown:user:{second.telegram_id}" in target_callbacks


async def test_truth_or_dare_view_uses_its_own_round_copy(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)
    names = {first.telegram_id: first.display_name, second.telegram_id: second.display_name}

    chooser_view = render_game(game, first.telegram_id, names)
    assert "جرئت یا حقیقت" in chooser_view
    assert "دور <b>1</b> از <b>3</b>" in chooser_view

    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)
    respondent_view = render_game(game, second.telegram_id, names)
    assert "<b>حقیقت 🗣</b>" in respondent_view
    assert "truth" not in respondent_view
    assert game_keyboard(game, second.telegram_id) is None
