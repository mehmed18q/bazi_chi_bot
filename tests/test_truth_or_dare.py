import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TelegramUser

from bazi_chi_bot.game import GameService, RepeatedQuestion, StaleAction
from bazi_chi_bot.handlers import build_router
from bazi_chi_bot.models import Game, GamePhase, GameStatus, GameType
from bazi_chi_bot.ui import render_game


async def test_truth_or_dare_rounds_response_review_and_score(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)
    assert game.phase is GamePhase.CHOICE
    assert game.challenge_asker_id == first.telegram_id
    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)
    assert game.phase is GamePhase.GUESSING
    assert game.challenge_respondent_id == second.telegram_id
    assert game.challenge_prompt_text
    game = await service.submit_challenge_response(game.id, second.telegram_id, "پاسخ من")
    assert game.phase is GamePhase.FINISHED
    game = await service.review_challenge(game.id, first.telegram_id, True, game.version)
    assert game.phase is GamePhase.CHOICE
    assert game.hand_number == 2
    assert (game.player1_score, game.player2_score) == (0, 1)
    assert (await service.get_stats(second.telegram_id)).points_won == 1
    game = await service.choose_challenge(game.id, second.telegram_id, "dare", game.version)
    game = await service.submit_challenge_response(game.id, first.telegram_id, "انجام شد")
    game = await service.review_challenge(game.id, second.telegram_id, False, game.version)
    assert game.challenge_approved is None
    assert game.status is GameStatus.ACTIVE
    assert (game.player1_score, game.player2_score) == (0, 1)


async def test_truth_or_dare_finishes_with_persisted_scores_and_correct_winner(service, players):
    first, second = players
    await service.add_question("truth", "یک حقیقت تازه بگو.")
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)

    for _ in range(3):
        game = await service.choose_challenge(
            game.id, game.challenge_asker_id, "truth", game.version
        )
        game = await service.submit_challenge_response(
            game.id, game.challenge_respondent_id, "پاسخ"
        )
        game = await service.review_challenge(game.id, game.challenge_asker_id, True, game.version)

    assert game.status is GameStatus.FINISHED
    assert (game.player1_score, game.player2_score) == (1, 2)
    assert game.winner_id == second.telegram_id
    assert game.loser_id == first.telegram_id
    names = {
        first.telegram_id: first.display_name,
        second.telegram_id: second.display_name,
    }
    respondent_view = render_game(game, game.challenge_respondent_id, names)
    asker_view = render_game(game, game.challenge_asker_id, names)
    assert "یک امتیاز گرفتی" in respondent_view
    assert f"{second.display_name} یک امتیاز گرفت" in asker_view
    assert "برنده شدی" in respondent_view
    assert "باختی" in asker_view


async def test_truth_or_dare_tie_has_no_artificial_winner(service, players):
    first, second = players
    await service.add_question("truth", "یک حقیقت دوم بگو.")
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)

    for _ in range(3):
        game = await service.choose_challenge(
            game.id, game.challenge_asker_id, "truth", game.version
        )
        game = await service.submit_challenge_response(
            game.id, game.challenge_respondent_id, "پاسخ"
        )
        game = await service.review_challenge(game.id, game.challenge_asker_id, False, game.version)

    assert game.status is GameStatus.FINISHED
    assert game.player1_score == game.player2_score == 0
    assert game.winner_id is game.loser_id is None
    for player in players:
        stats = await service.get_stats(player.telegram_id)
        assert (stats.games_played, stats.wins, stats.losses) == (1, 0, 0)
        assert "نتیجهٔ مساوی" in render_game(
            game,
            player.telegram_id,
            {first.telegram_id: first.display_name, second.telegram_id: second.display_name},
        )


async def test_simultaneous_challenge_reviews_score_exactly_once(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)
    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)
    game = await service.submit_challenge_response(game.id, second.telegram_id, "پاسخ")

    results = await asyncio.gather(
        service.review_challenge(game.id, first.telegram_id, True, game.version),
        service.review_challenge(game.id, first.telegram_id, False, game.version),
        return_exceptions=True,
    )

    assert sum(isinstance(result, Game) for result in results) == 1
    assert sum(isinstance(result, StaleAction) for result in results) == 1
    successful = next(result for result in results if isinstance(result, Game))
    updated = await service.get_game(game.id)
    assert updated.player1_score + updated.player2_score == successful.player2_score
    assert (await service.get_stats(second.telegram_id)).points_won == successful.player2_score


async def test_text_reply_selects_the_right_game_when_multiple_answers_are_pending(
    service, players
):
    first, second = players
    await service.set_user_activation(first.telegram_id, 999, True)
    await service.add_question("truth", "یک سؤال جایگزین.")

    final_game = await service.create_game(first.telegram_id, 2, 3)
    final_game = await service.join_game(final_game.invite_token, second.telegram_id)
    for _ in range(3):
        final_game = await service.hide_fist(
            final_game.id, final_game.hider_id, 1, final_game.version
        )
        final_game = (
            await service.guess_fist(final_game.id, final_game.guesser_id, 1, final_game.version)
        ).game
    final_game = await service.choose_final(
        final_game.id, first.telegram_id, "truth", final_game.version
    )

    direct_game = await service.create_game(second.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    direct_game = await service.join_game(direct_game.invite_token, first.telegram_id)
    direct_game = await service.choose_challenge(
        direct_game.id, second.telegram_id, "truth", direct_game.version
    )
    await service.save_game_message(direct_game.id, first.telegram_id, first.telegram_id, 900)

    router = build_router(service)
    handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "final_text_message"
    )
    message = SimpleNamespace(
        from_user=TelegramUser(id=first.telegram_id, is_bot=False, first_name=first.first_name),
        text="<جواب>",
        caption=None,
        reply_to_message=None,
        chat=SimpleNamespace(id=first.telegram_id),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    await handler(message, bot)
    assert "چند بازی" in message.answer.call_args.args[0]
    assert (await service.get_game(final_game.id)).final_response_text is None
    assert (await service.get_game(direct_game.id)).challenge_response_text is None

    message.reply_to_message = SimpleNamespace(message_id=900)
    await handler(message, bot)
    assert (await service.get_game(final_game.id)).final_response_text is None
    assert (await service.get_game(direct_game.id)).challenge_response_text == "<جواب>"
    assert "&lt;جواب&gt;" in bot.send_message.call_args.args[1]


async def test_truth_or_dare_uses_shared_nonrepeating_question_history(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)
    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)
    first_question = game.challenge_question_id
    game = await service.submit_challenge_response(game.id, second.telegram_id, "x")
    game = await service.review_challenge(game.id, first.telegram_id, False, game.version)
    game = await service.choose_challenge(game.id, second.telegram_id, "truth", game.version)
    # The opposite direction is a different respondent, so the same question is allowed.
    assert game.challenge_question_id == first_question


async def test_truth_or_dare_accepts_manual_prompt_when_question_bank_is_exhausted(
    service, players
):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)

    for _ in range(2):
        game = await service.choose_challenge(
            game.id, game.challenge_asker_id, "truth", game.version
        )
        game = await service.submit_challenge_response(
            game.id, game.challenge_respondent_id, "پاسخ"
        )
        game = await service.review_challenge(
            game.id, game.challenge_asker_id, False, game.version
        )

    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)
    assert game.phase is GamePhase.CHOICE
    assert game.challenge_kind.value == "truth"
    assert game.challenge_question_id is None
    assert game.challenge_prompt_text is None
    assert "متن سؤال" in render_game(
        game,
        first.telegram_id,
        {first.telegram_id: first.display_name, second.telegram_id: second.display_name},
    )

    game = await service.submit_challenge_prompt(
        game.id, first.telegram_id, "یک حقیقت دستی تازه بگو."
    )
    assert game.phase is GamePhase.GUESSING
    assert game.challenge_question_id is not None
    assert game.challenge_prompt_text == "یک حقیقت دستی تازه بگو."
    game = await service.submit_challenge_response(game.id, second.telegram_id, "پاسخ دستی")
    game = await service.review_challenge(game.id, first.telegram_id, True, game.version)
    assert game.status is GameStatus.FINISHED
    assert game.player2_score == 1


async def test_pending_manual_challenge_prompt_survives_service_restart(database, service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)
    for _ in range(2):
        game = await service.choose_challenge(
            game.id, game.challenge_asker_id, "truth", game.version
        )
        game = await service.submit_challenge_response(
            game.id, game.challenge_respondent_id, "پاسخ"
        )
        game = await service.review_challenge(
            game.id, game.challenge_asker_id, False, game.version
        )
    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)

    restarted = GameService(database, choose_first_hider=lambda candidates: candidates[0])
    recovered = next(item for item in await restarted.active_games(first.telegram_id) if item.id == game.id)
    assert recovered.challenge_kind.value == "truth"
    recovered = await restarted.submit_challenge_prompt(
        recovered.id, first.telegram_id, "بعد از راه‌اندازی دوباره"
    )
    assert recovered.phase is GamePhase.GUESSING


async def test_manual_challenge_prompt_rejects_pair_specific_duplicate(database, service, players):
    first, second = players
    manual_text = "این سؤال تکراری دستی است."
    question_id = await service.add_question("truth", manual_text)
    async with database.transaction() as connection:
        await connection.execute(
            "UPDATE questions SET active = CASE WHEN id = ? THEN 1 ELSE 0 END "
            "WHERE kind = 'truth'",
            (question_id,),
        )
    game = await service.create_game(first.telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    game = await service.join_game(game.invite_token, second.telegram_id)
    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)
    assert game.challenge_question_id == question_id
    game = await service.submit_challenge_response(game.id, second.telegram_id, "پاسخ")
    game = await service.review_challenge(game.id, first.telegram_id, False, game.version)
    game = await service.choose_challenge(game.id, second.telegram_id, "dare", game.version)
    game = await service.submit_challenge_response(game.id, first.telegram_id, "انجام شد")
    game = await service.review_challenge(game.id, second.telegram_id, False, game.version)
    game = await service.choose_challenge(game.id, first.telegram_id, "truth", game.version)

    with pytest.raises(RepeatedQuestion):
        await service.submit_challenge_prompt(game.id, first.telegram_id, manual_text)
