import time

import pytest

from bazi_chi_bot.daily_schedule import challenge_window, local_date
from bazi_chi_bot.errors import InvalidGameSetup, NotYourTurn, StaleAction
from bazi_chi_bot.game import GameService
from bazi_chi_bot.models import GameStatus, GameType
from bazi_chi_bot.reset_database import reset_history
from bazi_chi_bot.services.prediction import choose_rps_response, predict_choice
from bazi_chi_bot.telegram.keyboards import game_keyboard, game_types_keyboard, hands_keyboard
from bazi_chi_bot.telegram.rendering import render_game


async def test_two_player_moves_stay_hidden_and_rounds_are_persisted(service, players, database):
    first, second = players
    with pytest.raises(InvalidGameSetup):
        await service.create_game(first.telegram_id, 4, 3, GameType.ROCK_PAPER_SCISSORS)
    game = await service.create_game(first.telegram_id, 2, 3, GameType.ROCK_PAPER_SCISSORS)
    game = await service.join_game(game.invite_token, second.telegram_id)
    assert game.next_player_id == first.telegram_id
    assert game_keyboard(game, first.telegram_id) is not None
    assert game_keyboard(game, second.telegram_id) is None

    first_move = await service.play_rps(game.id, first.telegram_id, "rock", game.version)
    assert not first_move.round_finished
    game = first_move.game
    assert game.next_player_id == second.telegram_id
    assert game_keyboard(game, first.telegram_id) is None
    assert game_keyboard(game, second.telegram_id) is not None
    other_view = render_game(
        game,
        second.telegram_id,
        {
            first.telegram_id: "اول",
            second.telegram_id: "دوم",
        },
    )
    assert "حرکتت را انتخاب کن" not in other_view
    assert "✊ سنگ" not in other_view
    with pytest.raises((NotYourTurn, StaleAction)):
        await service.play_rps(game.id, first.telegram_id, "paper", game.version)

    result = await service.play_rps(game.id, second.telegram_id, "scissors", game.version)
    assert result.round_finished and result.point_winner_id == first.telegram_id
    assert (result.creator_move, result.player2_move) == ("rock", "scissors")
    game = result.game
    assert game.hand_number == 2 and game.player1_score == 1
    assert game.next_player_id == second.telegram_id
    assert (await GameService(database).get_game(game.id)).hand_number == 2
    async with database.connect() as connection:
        rows = await (
            await connection.execute(
                "SELECT user_id, choice FROM choice_observations WHERE game_id = ? ORDER BY user_id",
                (game.id,),
            )
        ).fetchall()
    assert [(row["user_id"], row["choice"]) for row in rows] == [
        (first.telegram_id, "rock"),
        (second.telegram_id, "scissors"),
    ]


async def test_rps_draw_counts_as_hand_and_match_can_end_draw(service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3, GameType.ROCK_PAPER_SCISSORS)
    game = await service.join_game(game.invite_token, second.telegram_id)
    for hand in range(3):
        starter = game.next_player_id
        game = (await service.play_rps(game.id, starter, "paper", game.version)).game
        result = await service.play_rps(game.id, game.next_player_id, "paper", game.version)
        game = result.game
        assert result.round_finished and result.point_winner_id is None
        if hand < 2:
            assert game.hand_number == hand + 2
    assert game.status is GameStatus.FINISHED
    assert game.winner_id is None and game.player1_score == game.player2_score == 0


async def test_rps_final_challenge_stays_visible_through_review(service, players):
    first, second = players
    names = {first.telegram_id: "اول", second.telegram_id: "دوم"}
    game = await service.create_game(first.telegram_id, 2, 3, GameType.ROCK_PAPER_SCISSORS)
    game = await service.join_game(game.invite_token, second.telegram_id)
    for _ in range(3):
        starter = game.next_player_id
        first_move = "rock" if starter == first.telegram_id else "scissors"
        game = (await service.play_rps(game.id, starter, first_move, game.version)).game
        second_move = "rock" if game.next_player_id == first.telegram_id else "scissors"
        game = (await service.play_rps(
            game.id, game.next_player_id, second_move, game.version
        )).game

    assert game.winner_id == first.telegram_id
    game = await service.choose_final(game.id, second.telegram_id, "truth", game.version)
    loser_view = render_game(game, second.telegram_id, names)
    assert "متن سؤال یا چالش" in loser_view
    assert "جوابت را همینجا بفرست" in loser_view

    game = await service.submit_final_response(game.id, second.telegram_id, "پاسخ من")
    winner_view = render_game(game, first.telegram_id, names)
    assert "پاسخ من" in winner_view
    assert "تأیید تو ۱ امتیاز" in winner_view

    game = await service.review_final_response(game.id, first.telegram_id, True, game.version)
    assert "۱ امتیاز برای پاسخ‌دهنده ثبت شد" in render_game(
        game, second.telegram_id, names
    )


async def test_solo_bot_commits_before_human_move_and_awards_match_once(service, players, database):
    player = players[0]
    game = (
        await service.create_solo_game(player.telegram_id, 2, 3, GameType.ROCK_PAPER_SCISSORS)
    ).game
    first_bot_move = game.rps_player2_move
    assert first_bot_move in ("rock", "paper", "scissors")
    assert game.next_player_id == player.telegram_id
    assert "✊ سنگ" not in render_game(
        game,
        player.telegram_id,
        {
            player.telegram_id: "بازیکن",
            -1: "ربات",
        },
    )
    beats = {"rock": "paper", "paper": "scissors", "scissors": "rock"}
    for hand in range(3):
        committed = game.rps_player2_move
        result = await service.play_rps(game.id, player.telegram_id, beats[committed], game.version)
        assert result.player2_move == committed
        game = result.game
        if hand < 2:
            assert game.rps_player2_move in beats
            assert game.rps_creator_move is None
    assert game.status is GameStatus.FINISHED
    assert game.winner_id == player.telegram_id
    assert (await service.get_stats(player.telegram_id)).points_won == 1
    assert (await GameService(database).get_game(game.id)).rps_player2_move == committed


async def test_prediction_uses_personal_history_for_both_games(
    service, players, database, monkeypatch
):
    first, second = players
    monkeypatch.setattr("bazi_chi_bot.services.prediction.secrets.randbelow", lambda n: n - 1)
    game = await service.create_game(first.telegram_id, 3, 3, GameType.GOL_YA_POOCH)
    rps_game = await service.create_game(first.telegram_id, 2, 3, GameType.ROCK_PAPER_SCISSORS)
    async with database.transaction() as connection:
        for game_id, game_type, option_count, first_choice, second_choice in (
            (game.id, "gol_ya_pooch", 3, "2", "1"),
            (rps_game.id, "rock_paper_scissors", 3, "rock", "scissors"),
        ):
            for index in range(1, 13):
                for user_id, choice in (
                    (first.telegram_id, first_choice),
                    (second.telegram_id, second_choice),
                ):
                    await connection.execute(
                        """INSERT INTO choice_observations
                           (game_id, hand_number, user_id, game_type, choice,
                            previous_choice, option_count, hand_bucket, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            game_id,
                            index,
                            user_id,
                            game_type,
                            choice,
                            choice if index > 1 else None,
                            option_count,
                            1,
                            int(time.time()),
                        ),
                    )
    async with database.connect() as connection:
        fist = await predict_choice(
            connection,
            user_id=first.telegram_id,
            game_type=GameType.GOL_YA_POOCH,
            options=("1", "2", "3"),
            hand_number=2,
            total_hands=3,
        )
        response = await choose_rps_response(connection, first.telegram_id, 2, 3)
    assert fist == "2"
    assert response == "paper"


async def test_rps_is_available_in_menus_and_daily_challenge(
    service, players, database, monkeypatch
):
    callbacks = {
        button.callback_data for row in game_types_keyboard().inline_keyboard for button in row
    }
    assert "setup:type:rps" in callbacks
    assert "setup:solo:type:rps" in {
        button.callback_data
        for row in game_types_keyboard(solo=True).inline_keyboard
        for button in row
    }
    assert "setup:rps:3" in {
        button.callback_data
        for row in hands_keyboard(2, GameType.ROCK_PAPER_SCISSORS).inline_keyboard
        for button in row
    }
    start, _ = challenge_window(local_date(int(time.time())))
    monkeypatch.setattr("time.time", lambda: start + 10)
    monkeypatch.setattr(
        "bazi_chi_bot.services.daily_challenges.secrets.choice",
        lambda items: (
            GameType.ROCK_PAPER_SCISSORS if GameType.ROCK_PAPER_SCISSORS in items else items[0]
        ),
    )
    await service.set_user_activation(players[0].telegram_id, 999, True)
    challenge = await service.daily.ensure_today()
    assert challenge.game_type is GameType.ROCK_PAPER_SCISSORS
    game = (await service.start_daily_challenge(players[0].telegram_id)).game
    assert game.game_type is GameType.ROCK_PAPER_SCISSORS
    assert game.rps_player2_move in ("rock", "paper", "scissors")
    beats = {"rock": "paper", "paper": "scissors", "scissors": "rock"}
    for _ in range(game.total_hands):
        game = (
            await service.play_rps(
                game.id, players[0].telegram_id, beats[game.rps_player2_move], game.version
            )
        ).game
    assert game.status is GameStatus.FINISHED
    assert game.winner_id == players[0].telegram_id
    assert (await service.get_stats(players[0].telegram_id)).points_won == 1
    assert (await service.start_daily_challenge(players[0].telegram_id)).game.id == game.id
    counts = await reset_history(database)
    assert counts["choice_observations"] == game.total_hands
