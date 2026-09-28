import asyncio

import pytest

from bazi_chi_bot.game import StaleAction
from bazi_chi_bot.models import TurnResult


async def test_repeated_hide_callback_is_idempotent(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 4, 3)
    game = await service.join_game(game.invite_token, second.telegram_id)
    old_version = game.version

    updated = await service.hide_fist(game.id, first.telegram_id, 4, old_version)
    assert updated.version == old_version + 1

    with pytest.raises(StaleAction):
        await service.hide_fist(game.id, first.telegram_id, 2, old_version)

    persisted = await service.get_game(game.id)
    assert persisted.hidden_fist == 4


async def test_simultaneous_guess_callbacks_score_exactly_once(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3)
    game = await service.join_game(game.invite_token, second.telegram_id)
    game = await service.hide_fist(game.id, first.telegram_id, 1, game.version)

    results = await asyncio.gather(
        service.guess_fist(game.id, second.telegram_id, 1, game.version),
        service.guess_fist(game.id, second.telegram_id, 1, game.version),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, TurnResult)]
    stale = [result for result in results if isinstance(result, StaleAction)]
    assert len(successes) == 1
    assert len(stale) == 1

    persisted = await service.get_game(game.id)
    assert persisted.player1_score + persisted.player2_score == 1
    stats = await service.get_stats(second.telegram_id)
    assert stats.correct_guesses == 1
    assert stats.points_won == 1


async def test_repeated_setup_callback_reuses_waiting_game(service, players):
    first, _ = players

    first_result, second_result = await asyncio.gather(
        service.create_game(first.telegram_id, 4, 5),
        service.create_game(first.telegram_id, 4, 5),
    )

    assert first_result.id == second_result.id
    games = await service.active_games(first.telegram_id)
    assert [game.id for game in games] == [first_result.id]
