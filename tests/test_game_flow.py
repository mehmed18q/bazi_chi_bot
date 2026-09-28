import pytest

from bazi_chi_bot.game import CannotJoinOwnGame, InvalidFinalMessage, InviteUnavailable, NotYourTurn
from bazi_chi_bot.models import FinalChoice, GamePhase, GameStatus


async def test_complete_match_alternates_roles_scores_and_records_stats(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, fists=3, total_hands=3)
    game = await service.join_game(game.invite_token, second.telegram_id)

    assert game.hider_id == first.telegram_id
    assert game.guesser_id == second.telegram_id
    assert game.phase is GamePhase.HIDING

    game = await service.hide_fist(
        game.id, first.telegram_id, fist=2, expected_version=game.version
    )
    turn1 = await service.guess_fist(
        game.id, second.telegram_id, fist=2, expected_version=game.version
    )
    assert turn1.correct is True
    assert turn1.point_winner_id == second.telegram_id
    assert turn1.game.hand_number == 2
    assert turn1.game.hider_id == second.telegram_id
    assert turn1.game.guesser_id == first.telegram_id

    game = await service.hide_fist(
        turn1.game.id, second.telegram_id, fist=1, expected_version=turn1.game.version
    )
    turn2 = await service.guess_fist(
        game.id, first.telegram_id, fist=3, expected_version=game.version
    )
    assert turn2.correct is False
    assert turn2.point_winner_id == second.telegram_id
    assert turn2.game.player2_score == 2
    assert turn2.game.hider_id == first.telegram_id

    game = await service.hide_fist(
        turn2.game.id, first.telegram_id, fist=3, expected_version=turn2.game.version
    )
    turn3 = await service.guess_fist(
        game.id, second.telegram_id, fist=3, expected_version=game.version
    )

    assert turn3.match_finished is True
    assert turn3.game.status is GameStatus.CHOICE
    assert turn3.game.phase is GamePhase.CHOICE
    assert turn3.game.winner_id == second.telegram_id
    assert turn3.game.loser_id == first.telegram_id
    assert (turn3.game.player1_score, turn3.game.player2_score) == (0, 3)

    first_stats = await service.get_stats(first.telegram_id)
    second_stats = await service.get_stats(second.telegram_id)
    assert (first_stats.games_played, first_stats.wins, first_stats.losses) == (1, 0, 1)
    assert (first_stats.correct_guesses, first_stats.wrong_guesses, first_stats.points_won) == (
        0,
        1,
        0,
    )
    assert (second_stats.games_played, second_stats.wins, second_stats.losses) == (1, 1, 0)
    assert (second_stats.correct_guesses, second_stats.wrong_guesses, second_stats.points_won) == (
        2,
        0,
        3,
    )

    finished = await service.choose_final(
        turn3.game.id,
        first.telegram_id,
        FinalChoice.TRUTH,
        expected_version=turn3.game.version,
    )
    assert finished.status is GameStatus.FINISHED
    assert finished.phase is GamePhase.FINISHED
    assert finished.final_choice is FinalChoice.TRUTH
    assert finished.final_prompt_text == "راستش را بگو، چرا مشت ۳ را انتخاب کردی؟"
    assert finished.final_question_id is not None
    assert finished.final_response_text is None

    with pytest.raises(NotYourTurn):
        await service.submit_final_prompt(finished.id, first.telegram_id, "سؤال اشتباه")

    with pytest.raises(NotYourTurn):
        await service.submit_final_prompt(finished.id, second.telegram_id, "سؤال جایگزین")
    prompted = finished
    assert prompted.final_prompt_text == "راستش را بگو، چرا مشت ۳ را انتخاب کردی؟"
    assert prompted.final_response_text is None

    assert await service.pending_final_response(first.telegram_id) == prompted
    assert await service.pending_final_prompt(second.telegram_id) is None

    with pytest.raises(NotYourTurn):
        await service.submit_final_response(prompted.id, second.telegram_id, "جواب اشتباه")
    with pytest.raises(InvalidFinalMessage):
        await service.submit_final_response(prompted.id, first.telegram_id, "  ")

    answered = await service.submit_final_response(
        prompted.id,
        first.telegram_id,
        "چون فکر کردم همان دست امن‌تر است.",
    )
    assert answered.final_response_text == "چون فکر کردم همان دست امن‌تر است."
    assert await service.pending_final_response(first.telegram_id) is None


async def test_invite_can_only_be_used_once_and_not_by_creator(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3)

    with pytest.raises(CannotJoinOwnGame):
        await service.join_game(game.invite_token, first.telegram_id)

    await service.join_game(game.invite_token, second.telegram_id)
    with pytest.raises(InviteUnavailable):
        await service.join_game(game.invite_token, 303)
