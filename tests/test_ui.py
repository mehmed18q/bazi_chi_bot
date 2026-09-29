from bazi_chi_bot.models import GamePhase, GameType
from bazi_chi_bot.ui import (
    START_TEXT,
    SUPPORT_TEXT,
    TRUTH_OR_DARE_RULE,
    admin_menu_keyboard,
    countdown_users_keyboard,
    game_keyboard,
    menu_keyboard,
    render_game,
    stats_text,
)


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
