import asyncio

import pytest

from bazi_chi_bot.game import GameService, PendingFinalResponse, RepeatedQuestion, StaleAction
from bazi_chi_bot.models import Game, GameType, User
from bazi_chi_bot.ui import render_game


async def finish(service, loser, winner, game_type=GameType.GOL_YA_POOCH):
    creator, guest = (loser, winner) if game_type is GameType.GOL_YA_POOCH else (winner, loser)
    game = await service.create_game(creator, 2, 3, game_type)
    game = await service.join_game(game.invite_token, guest)
    for _ in range(3):
        if game_type is GameType.GOL_YA_POOCH:
            game = await service.hide_fist(game.id, game.hider_id, 1, game.version)
            game = (await service.guess_fist(game.id, game.guesser_id, 1, game.version)).game
        else:
            for cell in (0, 3, 1, 4, 2):
                game = (
                    await service.place_mark(game.id, game.next_player_id, cell, game.version)
                ).game
    assert game.loser_id == loser and game.winner_id == winner
    return game


async def answer(service, game, choice="truth"):
    game = await service.choose_final(game.id, game.loser_id, choice, game.version)
    return await service.submit_final_response(game.id, game.loser_id, "پاسخ من")


async def test_ten_questions_never_repeat_across_games(database, service, players):
    a, b = [p.telegram_id for p in players]
    for index in range(9):
        await service.add_question("truth", f"سؤال {index}")
    seen = set()
    for index in range(10):
        kind = GameType.GOL_YA_POOCH if index % 2 else GameType.TIC_TAC_TOE
        game = await answer(service, await finish(service, a, b, kind))
        assert game.final_question_id not in seen
        seen.add(game.final_question_id)
    service = GameService(database, choose_first_hider=lambda ids: ids[0])
    game = await finish(service, a, b)
    game = await service.choose_final(game.id, a, "truth", game.version)
    assert game.final_question_id is None
    assert await service.pending_final_prompt(b) == game
    game = await service.submit_final_prompt(game.id, b, "سؤال دستی تازه")
    await service.submit_final_response(game.id, a, "پاسخ من")
    async with database.connect() as connection:
        rows = await (await connection.execute("SELECT * FROM question_answers")).fetchall()
    assert len(rows) == 11
    assert all(row["response_text"] == "پاسخ من" for row in rows)
    assert all(row["answered_at"] >= row["asked_at"] for row in rows)


async def test_reverse_respondent_and_different_opponent_can_get_same_question(service, players):
    a, b = [p.telegram_id for p in players]
    first = await answer(service, await finish(service, a, b))
    reverse = await answer(service, await finish(service, b, a))
    assert reverse.final_question_id == first.final_question_id
    await service.save_user(User(303, "third", "سوم", None, "سوم"))
    different = await answer(service, await finish(service, a, 303))
    assert different.final_question_id == first.final_question_id


async def test_empty_or_disabled_bank_asks_winner_for_prompt(database, service, players):
    a, b = [p.telegram_id for p in players]
    async with database.transaction() as connection:
        await connection.execute("UPDATE questions SET active = 0")
    game = await finish(service, a, b)
    game = await service.choose_final(game.id, a, "truth", game.version)
    assert game.final_question_id is None
    assert await service.pending_final_prompt(b) == game
    assert game in await service.active_games(b)
    game = await service.submit_final_prompt(game.id, b, "سؤال دستی")
    await service.submit_final_response(game.id, a, "جواب")
    question_id = await service.add_question("truth", "سؤال تازه <بله؟>")
    assert await service.add_question("truth", "  سؤال تازه <بله؟>  ") == question_id
    game = await finish(service, a, b)
    game = await service.choose_final(game.id, a, "truth", game.version)
    assert game.final_question_id == question_id
    assert "&lt;بله؟&gt;" in render_game(game, a, {})


async def test_question_reservation_is_atomic_and_recovers(database, service, players):
    a, b = [p.telegram_id for p in players]
    game = await finish(service, a, b)
    results = await asyncio.gather(
        *[service.choose_final(game.id, a, "truth", game.version) for _ in range(2)],
        return_exceptions=True,
    )
    assert sum(isinstance(result, StaleAction) for result in results) == 1
    assigned = next(result for result in results if isinstance(result, Game))
    service = GameService(database, choose_first_hider=lambda ids: ids[0])
    assert await service.pending_final_response(a) == assigned
    assert assigned in await service.active_games(a)
    other_game = await finish(service, a, b)
    with pytest.raises(PendingFinalResponse):
        await service.choose_final(other_game.id, a, "dare", other_game.version)
    answers = await asyncio.gather(
        *[service.submit_final_response(game.id, a, text) for text in ("اول", "دوم")],
        return_exceptions=True,
    )
    assert sum(isinstance(result, Game) for result in answers) == 1
    async with database.connect() as connection:
        rows = await (await connection.execute("SELECT * FROM question_answers")).fetchall()
    assert len(rows) == 1
    assert rows[0]["response_text"] == (await service.get_game(game.id)).final_response_text
    assert assigned.final_question_id == rows[0]["question_id"]


async def test_new_database_has_no_sample_questions(database):
    async with database.connect() as connection:
        row = await (await connection.execute("SELECT count(*) FROM questions")).fetchone()
    assert row[0] == 0


async def test_manual_prompts_obey_same_cross_game_history(database, service, players):
    a, b = [p.telegram_id for p in players]
    async with database.transaction() as connection:
        await connection.execute("UPDATE questions SET active = 0")
    first = await finish(service, a, b)
    first = await service.choose_final(first.id, a, "dare", first.version)
    first = await service.submit_final_prompt(first.id, b, "یک شعر بخوان")
    await service.submit_final_response(first.id, a, "خواندم")
    other = await finish(service, a, b, GameType.TIC_TAC_TOE)
    other = await service.choose_final(other.id, a, "dare", other.version)
    with pytest.raises(RepeatedQuestion):
        await service.submit_final_prompt(other.id, b, "  یک شعر بخوان  ")
    assert (await service.get_game(other.id)).final_prompt_text is None
    other = await service.submit_final_prompt(other.id, b, "یک داستان بگو")
    await service.submit_final_response(other.id, a, "داستان من")
    reverse = await finish(service, b, a)
    reverse = await service.choose_final(reverse.id, b, "dare", reverse.version)
    reverse = await service.submit_final_prompt(reverse.id, a, "یک شعر بخوان")
    assert reverse.final_question_id == first.final_question_id
    async with database.connect() as connection:
        question = await (
            await connection.execute(
                "SELECT active FROM questions WHERE id = ?",
                (first.final_question_id,),
            )
        ).fetchone()
    assert question["active"] == 0
