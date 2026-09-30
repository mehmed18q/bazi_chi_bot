import pytest

from bazi_chi_bot.game import InvalidGameSetup, InviteUnavailable
from bazi_chi_bot.models import GamePhase, GameStatus, GameType
from bazi_chi_bot.services.solo import choose_code_guess, choose_tic_tac_toe_cell, choose_word_guess
from bazi_chi_bot.telegram.keyboards import game_types_keyboard


async def test_solo_word_match_awards_only_one_point_for_match_win(service, players, monkeypatch):
    player, _ = players
    monkeypatch.setattr(
        "bazi_chi_bot.services.solo.choose_word_guess", lambda length, history: "ا" * length
    )
    started = await service.create_solo_game(player.telegram_id, 2, 3, GameType.WORD_GUESS)
    game = started.game
    assert game.is_solo and game.status is GameStatus.ACTIVE
    assert game.player2_id == -1
    assert game.phase is GamePhase.HIDING
    with pytest.raises(InviteUnavailable):
        await service.join_game(game.invite_token, players[1].telegram_id)

    await service.choose_word(game.id, player.telegram_id, "آب")
    game = (await service.advance_bot(game.id)).game
    assert game.hand_number == 2 and game.hider_id == -1
    assert game.word_secret is not None
    result = await service.guess_word(game.id, player.telegram_id, game.word_secret)
    assert result.round_finished
    await service.choose_word(game.id, player.telegram_id, "آب")
    finished = (await service.advance_bot(game.id)).game
    assert finished.status is GameStatus.FINISHED
    assert finished.winner_id == player.telegram_id
    assert finished.final_choice is None
    assert (await service.get_stats(player.telegram_id)).points_won == 1
    assert (await service.get_stats(-1)).points_won == 0
    assert (await service.advance_bot(game.id)).messages == ()
    assert (await service.get_stats(player.telegram_id)).points_won == 1
    assert all(entry.telegram_id != -1 for entry in await service.leaderboard(10))


async def test_solo_truth_or_dare_is_unavailable(service, players):
    with pytest.raises(InvalidGameSetup):
        await service.create_solo_game(players[0].telegram_id, 2, 3, GameType.TRUTH_OR_DARE)
    keys = [button.callback_data for row in game_types_keyboard(solo=True).inline_keyboard for button in row]
    assert "setup:type:tod" not in keys
    assert "setup:solo:type:random" in keys


async def test_solo_gol_robot_moves_without_guessing_hidden_fist(service, players, monkeypatch):
    player, _ = players
    monkeypatch.setattr("bazi_chi_bot.services.solo.secrets.randbelow", lambda maximum: 0)
    game = (await service.create_solo_game(player.telegram_id, 3, 3, GameType.GOL_YA_POOCH)).game
    await service.hide_fist(game.id, player.telegram_id, 3, game.version)
    advanced = await service.advance_bot(game.id)
    assert "اشتباه" in " ".join(advanced.messages)
    assert advanced.game.player1_score == 1
    assert (await service.get_stats(player.telegram_id)).points_won == 0


async def test_solo_robot_win_does_not_award_points(service, players, monkeypatch):
    player, _ = players
    monkeypatch.setattr("bazi_chi_bot.services.solo.secrets.randbelow", lambda maximum: 0)
    game = (await service.create_solo_game(player.telegram_id, 3, 3, GameType.GOL_YA_POOCH)).game
    await service.hide_fist(game.id, player.telegram_id, 1, game.version)
    game = (await service.advance_bot(game.id)).game
    result = await service.guess_fist(game.id, player.telegram_id, 2, game.version)
    game = (await service.advance_bot(result.game.id)).game
    await service.hide_fist(game.id, player.telegram_id, 1, game.version)
    finished = (await service.advance_bot(game.id)).game
    assert finished.status is GameStatus.FINISHED
    assert finished.winner_id == -1
    assert (await service.get_stats(player.telegram_id)).points_won == 0
    assert (await service.get_stats(-1)).points_won == 0


def test_bot_algorithms_only_accept_public_observations():
    assert choose_tic_tac_toe_cell("XX.OO....") == 5
    assert len(choose_word_guess(4, ())) == 4
    assert len(choose_code_guess(())) == 4


async def test_solo_tic_tac_toe_finishes_even_when_rounds_draw(service, players):
    player, _ = players
    game = (await service.create_solo_game(player.telegram_id, 2, 3, GameType.TIC_TAC_TOE)).game
    for _ in range(30):
        if game.status is GameStatus.FINISHED:
            break
        if game.next_player_id == -1:
            game = (await service.advance_bot(game.id)).game
            continue
        cell = game.board.index(".")
        result = await service.place_mark(game.id, player.telegram_id, cell, game.version)
        game = (await service.advance_bot(result.game.id)).game
    assert game.status is GameStatus.FINISHED
    assert game.final_choice is None
    assert (await service.get_stats(player.telegram_id)).points_won in (0, 1)


async def test_solo_mastermind_bot_uses_feedback_and_match_finishes(service, players):
    player, _ = players
    game = (await service.create_solo_game(player.telegram_id, 8, 3, GameType.MASTERMIND)).game
    for _ in range(8):
        if game.status is GameStatus.FINISHED:
            break
        if game.hider_id == player.telegram_id:
            game = await service.choose_mastermind_code(game.id, player.telegram_id, ("blue",) * 4)
            game = (await service.advance_bot(game.id)).game
        elif game.guesser_id == player.telegram_id:
            result = await service.guess_mastermind(
                game.id, player.telegram_id, game.mastermind_secret, game.version
            )
            game = (await service.advance_bot(result.game.id)).game
    assert game.status is GameStatus.FINISHED
    assert game.final_choice is None
