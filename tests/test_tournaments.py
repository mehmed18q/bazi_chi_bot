from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TelegramUser

from bazi_chi_bot.errors import InvalidGameSetup
from bazi_chi_bot.game import GameService
from bazi_chi_bot.models import GameStatus, GameType
from bazi_chi_bot.reset_database import reset_history
from bazi_chi_bot.services.tournaments import DUO_TYPES, SOLO_TYPES
from bazi_chi_bot.telegram.router import build_router
from bazi_chi_bot.telegram.shared import GamePresenter


async def _finish_rps(service, game, first_choice="rock", second_choice="scissors"):
    for _ in range(game.total_hands):
        starter = game.next_player_id
        move = first_choice if starter == game.creator_id else second_choice
        game = (await service.play_rps(game.id, starter, move, game.version)).game
        other_move = first_choice if game.next_player_id == game.creator_id else second_choice
        game = (await service.play_rps(game.id, game.next_player_id, other_move, game.version)).game
    return game


async def test_duo_tournament_keeps_friend_and_advances_once(service, players, database):
    first, second = players
    started = await service.create_tournament(
        first.telegram_id,
        False,
        (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH),
        (5, 7),
    )
    game = started.game
    assert game.total_hands == 5
    assert game.status is GameStatus.WAITING
    assert game.tournament_stage == 1
    game = await service.join_game(game.invite_token, second.telegram_id)
    assert game.status is GameStatus.ACTIVE
    tournament = await service.tournament_for_game(game.id)
    assert tournament.player2_id == second.telegram_id
    assert tournament.hand_counts == (5, 7)
    game = await _finish_rps(service, game)
    assert game.status is GameStatus.FINISHED
    assert game.final_choice is None

    next_game = await service.tournaments.sync_game(game.id)
    assert next_game.id != game.id
    assert next_game.game_type is GameType.GOL_YA_POOCH
    assert next_game.tournament_stage == 2
    assert next_game.player2_id == second.telegram_id
    assert next_game.status is GameStatus.ACTIVE
    assert next_game.total_hands == 7
    assert (await service.tournaments.sync_game(game.id)).id == next_game.id
    assert (await GameService(database).active_games(first.telegram_id))[0].id == next_game.id
    tournament = await service.tournament_for_game(next_game.id)
    assert (tournament.player1_score, tournament.player2_score) == (1, 0)

    for _ in range(next_game.total_hands):
        next_game = await service.hide_fist(next_game.id, next_game.hider_id, 1, next_game.version)
        next_game = (
            await service.guess_fist(next_game.id, next_game.guesser_id, 1, next_game.version)
        ).game
    assert next_game.status is GameStatus.FINISHED
    assert next_game.final_choice is None
    assert (await service.tournaments.sync_game(next_game.id)).id == next_game.id
    tournament = await service.tournament_for_game(next_game.id)
    assert tournament.status == "finished"
    assert tournament.player1_score + tournament.player2_score == 2
    assert (await service.tournaments.sync_game(next_game.id)).id == next_game.id
    async with database.connect() as connection:
        count = (
            await (
                await connection.execute(
                    "SELECT count(*) FROM games WHERE tournament_id = ?", (tournament.id,)
                )
            ).fetchone()
        )[0]
    assert count == 2


async def test_solo_tournament_commits_bot_moves_and_recovers_stage(service, players, database):
    player = players[0]
    game = (
        await service.create_tournament(
            player.telegram_id,
            True,
            (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH),
            (5, 9),
        )
    ).game
    assert game.total_hands == 5
    assert game.is_solo and game.rps_player2_move is not None
    beats = {"rock": "paper", "paper": "scissors", "scissors": "rock"}
    for _ in range(game.total_hands):
        game = (
            await service.play_rps(
                game.id, player.telegram_id, beats[game.rps_player2_move], game.version
            )
        ).game
    assert game.status is GameStatus.FINISHED
    restarted = GameService(database, choose_first_hider=lambda choices: choices[0])
    current = await restarted.current_tournament(player.telegram_id)
    assert current is not None
    tournament, next_game = current
    assert tournament.current_stage == 2
    assert next_game.game_type is GameType.GOL_YA_POOCH
    assert next_game.status is GameStatus.ACTIVE
    assert next_game.tournament_stage == 2
    assert next_game.total_hands == 9
    assert tournament.hand_counts == (5, 9)
    assert tournament.player1_score == 1


async def test_existing_tournament_without_hand_counts_keeps_three_hands(
    service, players, database
):
    first, second = players
    game = (await service.create_tournament(
        first.telegram_id, False,
        (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH),
    )).game
    async with database.transaction() as connection:
        await connection.execute(
            "UPDATE tournaments SET hand_counts_json = NULL WHERE id = ?",
            (game.tournament_id,),
        )
    restarted = GameService(database)
    game = await restarted.join_game(game.invite_token, second.telegram_id)
    assert (await restarted.tournament_for_game(game.id)).hand_counts == (3, 3)
    game = await _finish_rps(restarted, game)
    next_game = await restarted.tournaments.sync_game(game.id)
    assert next_game.total_hands == 3


async def test_tournament_selection_accepts_all_types_and_rejects_invalid(service, players):
    first, second = players
    with pytest.raises(InvalidGameSetup):
        await service.create_tournament(
            first.telegram_id,
            True,
            (GameType.ROCK_PAPER_SCISSORS, GameType.TRUTH_OR_DARE),
        )
    with pytest.raises(InvalidGameSetup):
        await service.create_tournament(
            first.telegram_id,
            False,
            (GameType.ROCK_PAPER_SCISSORS, GameType.ROCK_PAPER_SCISSORS),
        )
    duo = await service.create_tournament(first.telegram_id, False, DUO_TYPES)
    solo = await service.create_tournament(second.telegram_id, True, SOLO_TYPES)
    assert (await service.tournament_for_game(duo.game.id)).game_types == DUO_TYPES
    assert (await service.tournament_for_game(solo.game.id)).game_types == SOLO_TYPES


@pytest.mark.parametrize("counts", [(), (3,), (2, 3), (True, 3), (3, 5, 7)])
async def test_tournament_rejects_invalid_hand_counts(service, players, counts):
    with pytest.raises(InvalidGameSetup):
        await service.create_tournament(
            players[0].telegram_id,
            False,
            (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH),
            counts,
        )


async def test_tournament_wizard_count_and_order(service, players):
    router = build_router(service)
    handlers = {item.callback.__name__: item.callback for item in router.callback_query.handlers}
    callback = SimpleNamespace(
        data="tour:mode:s",
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await handlers["tournament_mode"](callback)
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    values = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert values == {
        "tour:count:s:2", "tour:count:s:3", "tour:count:s:4", "tour:count:s:5",
        "menu:tournament",
    }
    callback.data = "tour:mode:d"
    await handlers["tournament_mode"](callback)
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    values = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert "tour:count:d:6" in values
    assert "tour:count:d:7" not in values
    callback.data = "tour:count:d:6"
    await handlers["tournament_count"](callback)
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    values = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert "tour:pick:d:6:h" in values
    callback.data = "tour:count:s:5"
    await handlers["tournament_count"](callback)
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    values = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert "tour:pick:s:5:h" not in values
    callback.data = "tour:count:s:2"
    await handlers["tournament_count"](callback)
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    values = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert "tour:pick:s:2:g" in values
    assert "tour:pick:s:2:h" not in values
    callback.data = "tour:pick:s:2:g"
    await handlers["tournament_pick"](callback, SimpleNamespace())
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    values = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert "tour:pick:s:2:gr" in values
    assert "tour:pick:s:2:gg" not in values


async def test_tournament_reset_removes_progress(service, players, database):
    player = players[0]
    game = (
        await service.create_tournament(
            player.telegram_id,
            True,
            (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH),
        )
    ).game
    assert (await service.tournament_for_game(game.id)) is not None
    counts = await reset_history(database)
    assert counts["tournaments"] == 1
    async with database.connect() as connection:
        count = (await (await connection.execute("SELECT count(*) FROM tournaments")).fetchone())[0]
    assert count == 0


async def test_presenter_opens_next_stage_and_shows_tournament_score(service, players):
    first, second = players
    game = (
        await service.create_tournament(
            first.telegram_id,
            False,
            (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH),
        )
    ).game
    game = await service.join_game(game.invite_token, second.telegram_id)
    game = await _finish_rps(service, game)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=701)),
        delete_message=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    sent = await GamePresenter(service).send_game_view(
        bot, game, first.telegram_id, "⭐ بازی اول تمام شد", fresh=True
    )
    assert sent
    current = await service.current_tournament(first.telegram_id)
    assert current[1].game_type is GameType.GOL_YA_POOCH
    assert await service.game_message(current[1].id, first.telegram_id) == (first.telegram_id, 701)
    text = bot.send_message.call_args.args[1]
    assert "بازی بعدی تورنومنت شروع شد" in text
    assert "بازی 2 از 2" in text
    assert "برد بازی‌ها" in text


async def test_open_tournament_can_be_cancelled_and_restarted(service, players):
    first, second = players
    game = (
        await service.create_tournament(
            first.telegram_id,
            False,
            (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH),
        )
    ).game
    with pytest.raises(InvalidGameSetup):
        await service.create_tournament(
            first.telegram_id,
            False,
            (GameType.GOL_YA_POOCH, GameType.ROCK_PAPER_SCISSORS),
        )
    game = await service.join_game(game.invite_token, second.telegram_id)
    cancelled = await service.cancel_tournament(game.tournament_id, second.telegram_id)
    assert cancelled.status is GameStatus.CANCELLED
    assert (await service.tournament_for_game(game.id)).status == "cancelled"
    assert await service.current_tournament(first.telegram_id) is None
    replacement = (
        await service.create_tournament(
            first.telegram_id,
            True,
            (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH),
        )
    ).game
    assert replacement.tournament_id != game.tournament_id


async def test_last_wizard_choice_starts_solo_tournament(service, players):
    player = players[0]
    handlers = {
        item.callback.__name__: item.callback
        for item in build_router(service).callback_query.handlers
    }
    callback = SimpleNamespace(
        data="tour:pick:s:2:rg",
        from_user=TelegramUser(id=player.telegram_id, is_bot=False, first_name=player.first_name),
        message=SimpleNamespace(
            message_id=501,
            chat=SimpleNamespace(id=player.telegram_id),
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=502)),
        delete_message=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    await handlers["tournament_pick"](callback, bot)
    assert await service.current_tournament(player.telegram_id) is None
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    values = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert {"tour:hands:s:2:rg:3", "tour:hands:s:2:rg:5",
            "tour:hands:s:2:rg:7", "tour:hands:s:2:rg:9"} <= values

    callback.data = "tour:hands:s:2:rg:5"
    await handlers["tournament_hands"](callback, bot)
    assert await service.current_tournament(player.telegram_id) is None
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    values = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert "tour:hands:s:2:rg:59" in values

    callback.data = "tour:hands:s:2:rg:59"
    await handlers["tournament_hands"](callback, bot)
    current = await service.current_tournament(player.telegram_id)
    assert current is not None
    assert current[0].game_types == (GameType.ROCK_PAPER_SCISSORS, GameType.GOL_YA_POOCH)
    assert current[0].hand_counts == (5, 9)
    assert current[1].total_hands == 5
    assert "تورنومنت شروع شد" in bot.send_message.call_args.args[1]
    callback.answer.assert_awaited()


@pytest.mark.parametrize("game_type", DUO_TYPES)
async def test_each_game_can_be_first_duo_stage(service, players, game_type):
    first, second = players
    fallback = (
        GameType.GOL_YA_POOCH
        if game_type is not GameType.GOL_YA_POOCH
        else GameType.ROCK_PAPER_SCISSORS
    )
    game = (await service.create_tournament(first.telegram_id, False, (game_type, fallback))).game
    assert game.game_type is game_type
    game = await service.join_game(game.invite_token, second.telegram_id)
    assert game.status is GameStatus.ACTIVE
    assert game.player2_id == second.telegram_id
    assert game.total_hands == 3


@pytest.mark.parametrize("game_type", SOLO_TYPES)
async def test_each_game_can_be_first_solo_stage(service, players, game_type):
    player = players[0]
    fallback = (
        GameType.GOL_YA_POOCH
        if game_type is not GameType.GOL_YA_POOCH
        else GameType.ROCK_PAPER_SCISSORS
    )
    game = (await service.create_tournament(player.telegram_id, True, (game_type, fallback))).game
    assert game.game_type is game_type
    assert game.status is GameStatus.ACTIVE
    assert game.is_solo and game.player2_id == -1
    assert game.total_hands == 3
