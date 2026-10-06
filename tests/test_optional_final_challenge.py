import pytest

from bazi_chi_bot.game import GameService, NotYourTurn
from bazi_chi_bot.models import GamePhase, GameStatus, GameType
from bazi_chi_bot.ui import game_keyboard, render_game


@pytest.mark.parametrize(
    "game_type",
    [
        GameType.GOL_YA_POOCH,
        GameType.TIC_TAC_TOE,
        GameType.ROCK_PAPER_SCISSORS,
        GameType.WORD_GUESS,
        GameType.MASTERMIND,
    ],
)
async def test_two_player_game_without_final_challenge_finishes_immediately(
    database, service, players, game_type
):
    first, second = players
    fists = 8 if game_type is GameType.MASTERMIND else 2
    game = await service.create_game(
        first.telegram_id, fists, 3, game_type, final_challenge_enabled=False
    )
    assert not game.final_challenge_enabled
    service = GameService(database)
    assert not (await service.get_game(game.id)).final_challenge_enabled
    game = await service.join_game(game.invite_token, second.telegram_id)

    for _ in range(3):
        if game_type is GameType.GOL_YA_POOCH:
            game = await service.hide_fist(game.id, game.hider_id, 1, game.version)
            game = (await service.guess_fist(game.id, game.guesser_id, 1, game.version)).game
        elif game_type is GameType.TIC_TAC_TOE:
            for cell in (0, 3, 1, 4, 2):
                game = (await service.place_mark(
                    game.id, game.next_player_id, cell, game.version
                )).game
        elif game_type is GameType.ROCK_PAPER_SCISSORS:
            starter = game.next_player_id
            game = (await service.play_rps(game.id, starter, "rock", game.version)).game
            game = (await service.play_rps(
                game.id, game.next_player_id, "scissors", game.version
            )).game
        elif game_type is GameType.WORD_GUESS:
            game = await service.choose_word(game.id, game.hider_id, "کتاب")
            game = (await service.guess_word(game.id, game.guesser_id, "کتاب")).game
        else:
            colors = ("blue", "yellow", "black", "white")
            game = await service.choose_mastermind_code(game.id, game.hider_id, colors)
            game = (await service.guess_mastermind(
                game.id, game.guesser_id, colors, game.version
            )).game

    assert game.status is GameStatus.FINISHED
    assert game.phase is GamePhase.FINISHED
    assert game.winner_id is not None and game.loser_id is not None
    assert game.final_choice is None
    assert game_keyboard(game, game.loser_id) is None
    result_text = render_game(game, game.loser_id, {})
    assert "بازی تمام شد" in result_text or game_type is GameType.ROCK_PAPER_SCISSORS
    assert (await service.get_stats(game.winner_id)).wins == 1
    assert (await service.get_stats(game.loser_id)).losses == 1
    with pytest.raises(NotYourTurn):
        await service.choose_final(game.id, game.loser_id, "truth", game.version)


async def test_waiting_game_choice_is_part_of_game_identity(service, players):
    creator = players[0].telegram_id
    with_challenge = await service.create_game(creator, 2, 3)
    assert (await service.create_game(creator, 2, 3)).id == with_challenge.id

    without_challenge = await service.create_game(
        creator, 2, 3, final_challenge_enabled=False
    )
    assert without_challenge.id != with_challenge.id
    assert not without_challenge.final_challenge_enabled
    assert (await service.create_game(
        creator, 2, 3, final_challenge_enabled=False
    )).id == without_challenge.id
    assert "ندارد" in render_game(without_challenge, creator, {})
