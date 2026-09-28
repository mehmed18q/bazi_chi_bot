import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TelegramUser

from bazi_chi_bot.db import MIGRATIONS, Database
from bazi_chi_bot.game import GameService, InvalidCell, NotAPlayer, NotYourTurn, StaleAction
from bazi_chi_bot.handlers import build_router
from bazi_chi_bot.models import GameStatus, GameType
from bazi_chi_bot.telegram.shared import GamePresenter, notify_turn
from bazi_chi_bot.ui import game_keyboard, game_types_keyboard, render_game


async def start_game(service, players):
    game = await service.create_game(players[0].telegram_id, 2, 3, GameType.TIC_TAC_TOE)
    return await service.join_game(game.invite_token, players[1].telegram_id)


async def moves(service, game, cells):
    for cell in cells:
        result = await service.place_mark(game.id, game.next_player_id, cell, game.version)
        game = result.game
    return result


@pytest.mark.parametrize(
    "line",
    [
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    ],
)
async def test_all_winning_lines(service, players, line):
    game = await start_game(service, players)
    other = [cell for cell in range(9) if cell not in line]
    result = await moves(service, game, [line[0], other[0], line[1], other[1], line[2]])
    assert result.round_finished
    assert result.point_winner_id == players[0].telegram_id
    assert result.game.player1_score == 1
    assert result.game.hand_number == 2
    assert result.game.board == "........."
    assert result.game.next_player_id == players[1].telegram_id


async def test_draw_replays_round_without_points(service, players):
    game = await start_game(service, players)
    result = await moves(service, game, [0, 1, 2, 4, 3, 5, 7, 6, 8])
    assert result.round_finished and result.point_winner_id is None
    assert result.game.hand_number == 1
    assert result.game.player1_score == result.game.player2_score == 0
    assert result.game.board == "........."
    assert result.game.next_player_id == players[1].telegram_id
    assert (await service.get_stats(players[0].telegram_id)).games_played == 0


@pytest.mark.parametrize("choice", ["truth", "dare"])
async def test_complete_match_and_final_exchange_survive_restart(
    database, service, players, choice
):
    game = await start_game(service, players)
    for _ in range(3):
        result = await moves(service, game, [0, 3, 1, 4, 2])
        game = result.game
    assert game.status is GameStatus.CHOICE
    assert (game.player1_score, game.player2_score) == (2, 1)
    assert game.winner_id == players[0].telegram_id
    service = GameService(database)
    game = await service.choose_final(game.id, game.loser_id, choice, game.version)
    assert await service.pending_final_prompt(game.winner_id) is None
    assert game.final_question_id is not None
    assert (await service.pending_final_response(game.loser_id)).id == game.id
    game = await service.submit_final_response(game.id, game.loser_id, "پاسخ")
    assert await GameService(database).latest_finished_game(game.winner_id) == game
    stats = await service.get_stats(game.winner_id)
    assert (stats.games_played, stats.wins, stats.points_won) == (1, 1, 2)
    assert stats.correct_guesses == stats.wrong_guesses == 0
    assert (await service.get_stats(game.loser_id)).losses == 1


async def test_turn_validation_and_concurrent_callbacks(database, service, players):
    game = await start_game(service, players)
    with pytest.raises(NotAPlayer):
        await service.place_mark(game.id, 999, 0, game.version)
    with pytest.raises(NotYourTurn):
        await service.place_mark(game.id, players[1].telegram_id, 0, game.version)
    for cell in (-1, 9):
        with pytest.raises(InvalidCell):
            await service.place_mark(game.id, game.next_player_id, cell, game.version)
    with pytest.raises(NotYourTurn):
        await service.guess_fist(game.id, game.guesser_id, 1, game.version)
    results = await asyncio.gather(
        *[service.place_mark(game.id, game.next_player_id, cell, game.version) for cell in (0, 1)],
        return_exceptions=True,
    )
    assert sum(isinstance(result, StaleAction) for result in results) == 1
    recovered = await GameService(database).get_game(game.id)
    assert recovered.board.count("X") == 1
    assert recovered.version == game.version + 1
    assert recovered in await service.active_games(players[1].telegram_id)
    with pytest.raises(InvalidCell):
        await service.place_mark(
            game.id, recovered.next_player_id, recovered.board.index("X"), recovered.version
        )


async def test_game_types_have_independent_invitations(service, players):
    gol = await service.create_game(players[0].telegram_id, 2, 3)
    ttt = await service.create_game(players[0].telegram_id, 2, 3, GameType.TIC_TAC_TOE)
    assert gol.id != ttt.id
    assert (await service.get_game(gol.id)).status is GameStatus.WAITING
    assert await service.create_game(players[0].telegram_id, 2, 3, GameType.TIC_TAC_TOE) == ttt
    cancelled = await service.cancel_waiting(ttt.id, ttt.creator_id, ttt.version)
    assert cancelled.status is GameStatus.CANCELLED


async def test_board_ui_and_invitation(service, players):
    callbacks = [b.callback_data for row in game_types_keyboard().inline_keyboard for b in row]
    assert "setup:type:ttt" in callbacks and "setup:type:gol" in callbacks
    game = await start_game(service, players)
    keyboard = game_keyboard(game, game.next_player_id)
    assert [len(row) for row in keyboard.inline_keyboard] == [3, 3, 3]
    assert all(":move:" in b.callback_data for row in keyboard.inline_keyboard for b in row)
    assert game_keyboard(game, game.opponent_of(game.next_player_id)) is None
    text = render_game(game, game.creator_id, {game.creator_id: "<first>"})
    assert "دوز سه‌تایی" in text and "مشت" not in text and "&lt;first&gt;" in text


async def test_telegram_setup_and_move_notify_both_players(service, players):
    router = build_router(service)
    handlers = {
        handler.callback.__name__: handler.callback for handler in router.callback_query.handlers
    }
    callback = SimpleNamespace(
        from_user=TelegramUser(id=players[0].telegram_id, is_bot=False, first_name="صادق"),
        data="setup:ttt:3",
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot")),
        send_message=AsyncMock(),
    )
    await handlers["new_game"](callback)
    assert "کدام بازی" in callback.message.edit_text.call_args.args[0]
    await handlers["setup_tic_tac_toe"](callback)
    await handlers["create_tic_tac_toe"](callback, bot)
    game = (await service.active_games(players[0].telegram_id))[0]
    assert game.game_type is GameType.TIC_TAC_TOE
    assert "لینک ورود" in callback.message.edit_text.call_args.args[0]
    game = await service.join_game(game.invite_token, players[1].telegram_id)
    callback.data = f"game:{game.id}:{game.version}:move:0"
    await handlers["game_action"](callback, bot)
    updated = await service.get_game(game.id)
    assert updated.board == "X........"
    assert "منتظر حرکت" in callback.message.edit_text.call_args.args[0]
    assert bot.send_message.call_args.args[0] == players[1].telegram_id
    assert "نوبت توست" in bot.send_message.call_args.args[1]


async def test_game_presenter_edits_the_saved_card_instead_of_sending_again(service, players):
    game = await start_game(service, players)
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=700)),
        edit_message_text=AsyncMock(),
    )
    presenter = GamePresenter(service)

    await presenter.send_game_view(bot, game, players[1].telegram_id)
    await presenter.send_game_view(bot, game, players[1].telegram_id)

    bot.send_message.assert_awaited_once()
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.call_args.kwargs["chat_id"] == players[1].telegram_id
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 700


async def test_turn_notification_is_removed_after_delivery():
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=701)),
        delete_message=AsyncMock(),
    )

    await notify_turn(bot, 202, delete_after=0)
    await asyncio.sleep(0.01)

    bot.send_message.assert_awaited_once()
    bot.delete_message.assert_awaited_once_with(202, 701)


async def test_existing_database_migrates_without_losing_games(tmp_path):
    database = Database(tmp_path / "old.sqlite3")
    async with database.connect() as connection:
        await connection.executescript("\n".join(MIGRATIONS[:3]) + "\nPRAGMA user_version = 3;")
        await connection.execute(
            "INSERT INTO users (telegram_id, first_name, display_name, created_at, updated_at) "
            "VALUES (101, 'first', 'first', 0, 0)"
        )
        await connection.execute(
            "INSERT INTO games (invite_token, creator_id, fists, total_hands, created_at, updated_at) "
            "VALUES ('existing', 101, 2, 3, 0, 0)"
        )
        await connection.commit()
    await database.initialize()
    game = await GameService(database).get_game_by_token("existing")
    assert game.game_type is GameType.GOL_YA_POOCH
    assert game.status is GameStatus.WAITING
