from bazi_chi_bot.reset_database import reset_history


async def test_reset_preserves_users_and_questions(database, service, players):
    first, second = players
    question_id = await service.add_question("truth", "سؤال ماندگار")
    game = await service.create_game(first.telegram_id, 2, 3)
    await service.join_game(game.invite_token, second.telegram_id)
    await service.set_pending_invite(first.telegram_id, "stale-invite")
    counts = await reset_history(database)
    assert counts["users_preserved"] == 2
    assert counts["questions_preserved"] == 3
    assert await service.get_user(first.telegram_id) == first
    assert await service.active_games(first.telegram_id) == []
    stats = await service.get_stats(first.telegram_id)
    assert stats.games_played == stats.points_won == 0
    async with database.connect() as connection:
        row = await (await connection.execute("SELECT id, text FROM questions WHERE id = ?", (question_id,))).fetchone()
        assert row["text"] == "سؤال ماندگار"
        assert (await (await connection.execute("SELECT count(*) FROM games")).fetchone())[0] == 0
