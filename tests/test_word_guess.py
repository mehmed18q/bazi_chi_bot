from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TelegramUser

from bazi_chi_bot.game import GameService, InvalidWord, InvalidWordLength
from bazi_chi_bot.handlers import build_router
from bazi_chi_bot.models import GamePhase, GameStatus, GameType
from bazi_chi_bot.rules import evaluate_word_guess, normalize_word
from bazi_chi_bot.ui import game_keyboard, game_types_keyboard, hands_keyboard, render_game


def test_word_normalization_and_duplicate_letter_feedback():
    assert normalize_word("  كتاب  ") == "کتاب"
    assert evaluate_word_guess("کتاب", "بابک") == "yyby"
    with pytest.raises(InvalidWord):
        normalize_word("دو کلمه")
    with pytest.raises(InvalidWordLength):
        normalize_word("آب", expected_length=4)


async def test_complete_word_match_scores_swaps_roles_and_runs_final_challenge(
    database, service, players
):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.WORD_GUESS)
    game = await service.join_game(game.invite_token, second.telegram_id)
    assert game.phase is GamePhase.HIDING
    assert game.hider_id == first.telegram_id
    assert game_keyboard(game, first.telegram_id) is None

    game = await service.choose_word(game.id, first.telegram_id, "كتاب")
    assert game.word_secret == "کتاب"
    assert len(game.word_secret) == 4
    assert game.phase is GamePhase.GUESSING
    assert game_keyboard(game, second.telegram_id) is None

    names = {
        first.telegram_id: first.display_name,
        second.telegram_id: second.display_name,
    }
    assert "کتاب" in render_game(game, first.telegram_id, names)
    guesser_view = render_game(game, second.telegram_id, names)
    assert "کتاب" not in guesser_view
    assert "4 حرفی" in guesser_view and "4</b>" in guesser_view

    for attempt, guess in enumerate(("خانه", "دریا"), start=1):
        result = await service.guess_word(game.id, second.telegram_id, guess)
        game = result.game
        assert not result.round_finished
        assert game.word_attempts == attempt
        assert len(game.word_guesses) == attempt

    game = await GameService(database).get_game(game.id)
    assert [item.text for item in game.word_guesses] == ["خانه", "دریا"]

    for guess in ("مادر", "سالم"):
        result = await service.guess_word(game.id, second.telegram_id, guess)
        game = result.game
    assert result.round_finished and not result.guessed_correctly
    assert result.point_winner_id == first.telegram_id
    assert game.hand_number == 2
    assert game.phase is GamePhase.HIDING
    assert game.hider_id == second.telegram_id
    assert game.word_secret is None
    assert (game.player1_score, game.player2_score) == (1, 0)

    game = await service.choose_word(game.id, second.telegram_id, "خانه")
    result = await service.guess_word(game.id, first.telegram_id, "خانه")
    game = result.game
    assert result.guessed_correctly
    assert game.hider_id == first.telegram_id
    assert (game.player1_score, game.player2_score) == (2, 0)

    game = await service.choose_word(game.id, first.telegram_id, "سیب")
    result = await service.guess_word(game.id, second.telegram_id, "سیب")
    game = result.game
    assert result.match_finished
    assert game.status is GameStatus.CHOICE
    assert game.winner_id == first.telegram_id
    assert game.loser_id == second.telegram_id
    assert (game.player1_score, game.player2_score) == (2, 1)

    winner_stats = await service.get_stats(first.telegram_id)
    loser_stats = await service.get_stats(second.telegram_id)
    assert (winner_stats.games_played, winner_stats.wins, winner_stats.points_won) == (1, 1, 2)
    assert (loser_stats.games_played, loser_stats.losses, loser_stats.points_won) == (1, 1, 1)
    assert winner_stats.correct_guesses == winner_stats.wrong_guesses == 0

    game = await service.choose_final(game.id, second.telegram_id, "truth", game.version)
    game = await service.submit_final_response(game.id, second.telegram_id, "پاسخ آخر")
    game = await service.review_final_response(game.id, first.telegram_id, True, game.version)
    assert game.final_response_approved is True
    assert (game.player1_score, game.player2_score) == (2, 1)
    assert (await service.get_stats(second.telegram_id)).points_won == 2


async def test_word_setup_and_live_attempt_count_notifications(service, players):
    callbacks = {
        button.callback_data for row in game_types_keyboard().inline_keyboard for button in row
    }
    assert "setup:type:word" in callbacks
    round_callbacks = {
        button.callback_data
        for row in hands_keyboard(2, GameType.WORD_GUESS).inline_keyboard
        for button in row
    }
    assert {"setup:word:3", "setup:word:5", "setup:word:7", "setup:word:9"} <= round_callbacks

    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.WORD_GUESS)
    game = await service.join_game(game.invite_token, second.telegram_id)
    router = build_router(service)
    handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "final_text_message"
    )
    bot = SimpleNamespace(send_message=AsyncMock(return_value=None))

    choose_message = SimpleNamespace(
        from_user=TelegramUser(id=first.telegram_id, is_bot=False, first_name=first.first_name),
        text="کتاب",
        caption=None,
        reply_to_message=None,
        chat=SimpleNamespace(id=first.telegram_id),
        answer=AsyncMock(),
    )
    await handler(choose_message, bot)
    assert "کلمهٔ 4 حرفی" in choose_message.answer.call_args.args[0]
    sent_texts = [call.args[1] for call in bot.send_message.await_args_list]
    assert any("4 حرفی" in text and "4</b>" in text for text in sent_texts)
    assert any("4 حرف و 4 فرصت" in text for text in sent_texts)

    guess_message = SimpleNamespace(
        from_user=TelegramUser(id=second.telegram_id, is_bot=False, first_name=second.first_name),
        text="خانه",
        caption=None,
        reply_to_message=None,
        chat=SimpleNamespace(id=second.telegram_id),
        answer=AsyncMock(),
    )
    bot.send_message.reset_mock()
    await handler(guess_message, bot)
    assert "<b>3</b> فرصت" in guess_message.answer.call_args.args[0]
    sent_texts = [call.args[1] for call in bot.send_message.await_args_list]
    assert any("فرصت باقی‌مانده: <b>3</b>" in text for text in sent_texts)
