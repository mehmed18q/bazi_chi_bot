from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import User as TelegramUser

from bazi_chi_bot.handlers import build_router
from bazi_chi_bot.models import GamePhase, GameStatus, GameType, WordGuess
from bazi_chi_bot.telegram.shared import (
    GamePresenter,
    send_full_game_text_if_needed,
    word_round_result_text,
)
from bazi_chi_bot.ui import render_game, word_guess_board


async def test_fresh_game_card_replaces_the_previous_message(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3)
    game = await service.join_game(game.invite_token, second.telegram_id)
    await service.save_game_message(game.id, first.telegram_id, first.telegram_id, 501)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=502)),
        delete_message=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    sent = await GamePresenter(service).send_game_view(
        bot, game, first.telegram_id, "🎲 نوبت تازه", fresh=True
    )

    assert sent is True
    assert await service.game_message(game.id, first.telegram_id) == (first.telegram_id, 502)
    assert "🎲 نوبت تازه" in bot.send_message.call_args.args[1]
    bot.delete_message.assert_awaited_once_with(first.telegram_id, 501)
    bot.edit_message_text.assert_not_awaited()


async def test_truth_or_dare_review_shows_result_to_both_and_opens_next_turn(
    service, players
):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)
    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)
    game = await service.submit_challenge_response(game.id, second.telegram_id, "پاسخ")
    await service.save_game_message(game.id, first.telegram_id, first.telegram_id, 501)
    await service.save_game_message(game.id, second.telegram_id, second.telegram_id, 601)

    action = next(
        handler.callback
        for handler in build_router(service).callback_query.handlers
        if handler.callback.__name__ == "game_action"
    )
    callback = SimpleNamespace(
        from_user=TelegramUser(id=first.telegram_id, is_bot=False, first_name="صادق"),
        data=f"game:{game.id}:{game.version}:challenge_review:yes",
        message=SimpleNamespace(
            message_id=501,
            chat=SimpleNamespace(id=first.telegram_id),
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=(SimpleNamespace(message_id=502), SimpleNamespace(message_id=602))
        ),
        delete_message=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await action(callback, bot)

    assert (await service.get_game(game.id)).hand_number == 2
    assert await service.game_message(game.id, first.telegram_id) == (first.telegram_id, 502)
    assert await service.game_message(game.id, second.telegram_id) == (second.telegram_id, 602)
    texts = [call.args[1] for call in bot.send_message.await_args_list]
    assert all("پاسخ تأیید شد" in text and "دور تازه شروع شد" in text for text in texts)
    assert "نوبت انتخاب توست" in texts[1]
    assert bot.delete_message.await_count == 2


async def test_word_guess_feedback_and_mastermind_progress_are_visible(service, players):
    first, second = players
    names = {first.telegram_id: first.display_name, second.telegram_id: second.display_name}

    word = await service.create_game(first.telegram_id, 2, 3, GameType.WORD_GUESS)
    word = await service.join_game(word.invite_token, second.telegram_id)
    word = await service.choose_word(word.id, first.telegram_id, "کتاب")
    result = await service.guess_word(word.id, second.telegram_id, "خانه")
    feedback = word_round_result_text(result, names)
    assert "درست نبود" in feedback
    assert "فرصت باقی‌مانده: <b>3</b>" in feedback
    assert "⬛" in feedback

    mastermind = await service.create_game(first.telegram_id, 8, 3, GameType.MASTERMIND)
    mastermind = await service.join_game(mastermind.invite_token, second.telegram_id)
    mastermind = await service.choose_mastermind_code(
        mastermind.id, first.telegram_id, ("blue", "yellow", "black", "white")
    )
    guess = await service.guess_mastermind(
        mastermind.id, second.telegram_id, ("green", "red", "black", "white")
    )
    view = render_game(guess.game, second.telegram_id, names)
    assert "تلاش باقی‌مانده: <b>7</b> | 📋 ردیف‌های انجام‌شده: <b>1</b> از <b>8</b>" in view


def test_word_guess_history_keeps_recent_rows_within_one_message():
    guesses = tuple(WordGuess("آ" * 20, "b" * 20) for _ in range(20))
    board = word_guess_board(guesses)
    assert board.startswith("… 12 حدس قبلی")
    assert board.count("⬛") == 8 * 20
    assert len(board) < 3000


async def test_long_questions_and_answers_fit_the_card_and_keep_full_text(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)
    long_prompt = "پ" * 3000
    long_answer = "ج" * 3000
    game = replace(
        game,
        phase=GamePhase.FINISHED,
        status=GameStatus.ACTIVE,
        challenge_prompt_text=long_prompt,
        challenge_response_text=long_answer,
    )
    names = {first.telegram_id: first.display_name, second.telegram_id: second.display_name}
    card = render_game(game, first.telegram_id, names)
    assert len(card) < 4096
    assert "متن کامل در پیام جداگانه" in card

    bot = SimpleNamespace(send_message=AsyncMock())
    await send_full_game_text_if_needed(bot, second.telegram_id, game, "سؤال", long_prompt)
    assert long_prompt in bot.send_message.call_args.args[1]
