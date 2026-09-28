import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TelegramUser

from bazi_chi_bot.game import GameService, NotAPlayer, NotYourTurn, StaleAction
from bazi_chi_bot.handlers import build_router
from bazi_chi_bot.models import Game, GameType
from bazi_chi_bot.ui import game_keyboard, render_game


async def answered_game(service, players, game_type, choice):
    game = await service.create_game(players[0].telegram_id, 2, 3, game_type)
    game = await service.join_game(game.invite_token, players[1].telegram_id)
    for _ in range(3):
        if game_type is GameType.GOL_YA_POOCH:
            game = await service.hide_fist(game.id, game.hider_id, 1, game.version)
            game = (await service.guess_fist(game.id, game.guesser_id, 1, game.version)).game
        else:
            for cell in (0, 3, 1, 4, 2):
                game = (
                    await service.place_mark(game.id, game.next_player_id, cell, game.version)
                ).game
    game = await service.choose_final(game.id, game.loser_id, choice, game.version)
    with pytest.raises(NotYourTurn):
        await service.review_final_response(game.id, game.winner_id, True, game.version)
    return await service.submit_final_response(game.id, game.loser_id, "انجام شد")


@pytest.mark.parametrize("game_type", [GameType.GOL_YA_POOCH, GameType.TIC_TAC_TOE])
@pytest.mark.parametrize("choice", ["truth", "dare"])
@pytest.mark.parametrize("approved", [True, False])
async def test_review_scores_once_and_recovers(
    database, service, players, game_type, choice, approved
):
    game = await answered_game(service, players, game_type, choice)
    before = await service.get_stats(game.loser_id)
    winner_before = await service.get_stats(game.winner_id)
    service = GameService(database)
    assert game in await service.active_games(game.winner_id)
    assert game_keyboard(game, game.winner_id) is not None
    assert game_keyboard(game, game.loser_id) is None
    with pytest.raises(NotAPlayer):
        await service.review_final_response(game.id, 999, approved, game.version)
    with pytest.raises(NotYourTurn):
        await service.review_final_response(game.id, game.loser_id, approved, game.version)
    result = await service.review_final_response(game.id, game.winner_id, approved, game.version)
    assert result.final_response_approved is approved
    assert result.final_reviewed_at is not None
    with pytest.raises(StaleAction):
        await service.review_final_response(game.id, game.winner_id, approved, game.version)
    with pytest.raises(NotYourTurn):
        await service.review_final_response(game.id, game.winner_id, not approved, result.version)
    after = await service.get_stats(game.loser_id)
    assert after.points_won == before.points_won + int(approved)
    assert (after.wins, after.losses, after.games_played) == (
        before.wins,
        before.losses,
        before.games_played,
    )
    assert await service.get_stats(game.winner_id) == winner_before
    assert (result.player1_score, result.player2_score) == (game.player1_score, game.player2_score)
    assert result not in await service.active_games(game.winner_id)
    assert game_keyboard(result, game.winner_id) is None
    assert await GameService(database).get_game(game.id) == result
    text = render_game(result, game.loser_id, {})
    assert ("۱ امتیاز به مجموع" if approved else "امتیازی اضافه نشد") in text


async def test_simultaneous_reviews_apply_only_once(service, players):
    game = await answered_game(service, players, GameType.GOL_YA_POOCH, "truth")
    before = await service.get_stats(game.loser_id)
    results = await asyncio.gather(
        *[
            service.review_final_response(game.id, game.winner_id, approved, game.version)
            for approved in (True, False, True)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, StaleAction) for result in results) == 2
    result = next(result for result in results if isinstance(result, Game))
    assert (await service.get_stats(game.loser_id)).points_won == (
        before.points_won + int(result.final_response_approved)
    )


async def test_review_callback_notifies_respondent(service, players):
    game = await answered_game(service, players, GameType.TIC_TAC_TOE, "dare")
    router = build_router(service)
    action = next(
        h.callback for h in router.callback_query.handlers if h.callback.__name__ == "game_action"
    )
    callback = SimpleNamespace(
        from_user=TelegramUser(id=game.winner_id, is_bot=False, first_name="برنده"),
        data=f"game:{game.id}:{game.version}:review:yes",
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    await action(callback, bot)
    assert "۱ امتیاز به مجموع" in callback.message.edit_text.call_args.args[0]
    assert bot.send_message.call_args.args[0] == game.loser_id
    assert "۱ امتیاز به مجموع" in bot.send_message.call_args.args[1]
    assert bot.send_message.call_args.kwargs["reply_markup"] is None
