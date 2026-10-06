import aiosqlite
import pytest

from bazi_chi_bot.db import MIGRATIONS, Database
from bazi_chi_bot.game import GameService
from bazi_chi_bot.persistence.scores import award_point


@pytest.mark.parametrize("version", range(1, len(MIGRATIONS)))
async def test_upgrade_every_previous_schema_preserves_data_and_scores(tmp_path, version):
    database = Database(tmp_path / f"v{version}.sqlite3")
    async with database.connect() as connection:
        await connection.executescript(
            "\n".join(MIGRATIONS[:version]) + f"\nPRAGMA user_version = {version};"
        )
        await connection.execute(
            "INSERT INTO users (telegram_id, first_name, display_name, created_at, updated_at) "
            "VALUES (101, 'first', 'first', 0, 0)"
        )
        if version >= 30:
            # Older upgrade paths assign this ID in a migration; the latest schema
            # expects new rows to receive it through the user service.
            await connection.execute("UPDATE users SET id = 1 WHERE telegram_id = 101")
        await connection.execute(
            "INSERT INTO user_stats (telegram_id, points_won) VALUES (101, ?)",
            (0 if version >= 30 else 17,),
        )
        if version >= 30:
            # Version 30 already stores points in the event ledger.
            await connection.execute(
                """INSERT INTO score_events (user_id, reason, amount, created_at)
                   VALUES (101, 'legacy', 17, unixepoch())"""
            )
        await connection.execute(
            "INSERT INTO games (invite_token, creator_id, fists, total_hands, created_at, updated_at) "
            "VALUES ('original', 101, 2, 3, 0, 0)"
        )
        await connection.commit()
    await database.initialize()
    await database.initialize()
    service = GameService(database)
    migrated_game = await service.get_game_by_token("original")
    assert migrated_game.creator_id == 101
    assert migrated_game.final_challenge_enabled
    assert (await service.get_stats(101)).points_won == 17
    migrated_user = await service.get_user(101)
    assert migrated_user is not None and migrated_user.id == 1
    assert not migrated_user.is_activated
    assert migrated_user.nickname == "first"
    assert migrated_user.telegram_display_name == "first"
    assert not migrated_user.nickname_is_custom
    async with database.connect() as connection:
        events = await (await connection.execute("SELECT * FROM score_events")).fetchall()
        assert len(events) == 1
        assert events[0]["reason"] == "legacy" and events[0]["amount"] == 17
        assert await (await connection.execute("PRAGMA foreign_key_check")).fetchall() == []
        assert (await (await connection.execute("PRAGMA integrity_check")).fetchone())[0] == "ok"


async def test_score_ledger_and_aggregate_are_atomic_and_duplicate_safe(database, service, players):
    a, b = [player.telegram_id for player in players]
    game = await service.create_game(a, 2, 3)
    game = await service.join_game(game.invite_token, b)
    for _ in range(3):
        game = await service.hide_fist(game.id, game.hider_id, 1, game.version)
        game = (await service.guess_fist(game.id, game.guesser_id, 1, game.version)).game
    game = await service.choose_final(game.id, game.loser_id, "truth", game.version)
    game = await service.submit_final_response(game.id, game.loser_id, "جواب")
    game = await service.review_final_response(game.id, game.winner_id, True, game.version)

    async with database.connect() as connection:
        events = await (await connection.execute("SELECT * FROM score_events")).fetchall()
    assert len(events) == 4
    assert sum(row["reason"] == "round" for row in events) == 3
    assert sum(row["reason"] == "challenge" for row in events) == 1
    for user_id in (a, b):
        assert (await service.get_stats(user_id)).points_won == sum(
            row["amount"] for row in events if row["user_id"] == user_id
        )
    before = await service.get_stats(game.loser_id)
    with pytest.raises(aiosqlite.IntegrityError):
        async with database.transaction() as connection:
            await award_point(connection, game.id, game.loser_id, "challenge")
    assert await service.get_stats(game.loser_id) == before

    with pytest.raises(RuntimeError):
        async with database.transaction() as connection:
            await award_point(connection, game.id, game.loser_id, "round", 99)
            raise RuntimeError("simulate a failed transition")
    assert await service.get_stats(game.loser_id) == before
    async with database.connect() as connection:
        assert (await (await connection.execute("SELECT count(*) FROM score_events")).fetchone())[
            0
        ] == 4
    with pytest.raises(aiosqlite.IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(
                "UPDATE score_events SET amount = 10 WHERE id = ?", (events[0]["id"],)
            )


async def test_old_null_hand_challenge_scores_are_migrated(tmp_path):
    # Schema 12 is the last released version whose challenge constraint required NULL.
    old_version = 12
    database = Database(tmp_path / "old-challenge-check.sqlite3")
    async with database.connect() as connection:
        await connection.executescript(
            "\n".join(MIGRATIONS[:old_version]) + f"\nPRAGMA user_version = {old_version};"
        )
        await connection.executemany(
            "INSERT INTO users (telegram_id, first_name, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 0)",
            [(101, "first", "first"), (202, "second", "second")],
        )
        await connection.executemany(
            "INSERT INTO user_stats (telegram_id) VALUES (?)", [(101,), (202,)]
        )
        cursor = await connection.execute(
            "INSERT INTO games (invite_token, creator_id, player2_id, fists, total_hands, "
            "created_at, updated_at) VALUES ('old', 101, 202, 2, 3, 0, 0)"
        )
        game_id = cursor.lastrowid
        await connection.execute(
            "INSERT INTO score_events "
            "(user_id, game_id, reason, hand_number, amount, created_at) "
            "VALUES (202, ?, 'challenge', NULL, 1, 0)",
            (game_id,),
        )
        await connection.commit()

    await database.initialize()

    async with database.transaction() as connection:
        migrated = await (
            await connection.execute(
                "SELECT hand_number FROM score_events WHERE reason = 'challenge'"
            )
        ).fetchone()
        assert migrated["hand_number"] == 1
        await award_point(connection, game_id, 202, "challenge", 2)


async def test_direct_truth_or_dare_scores_are_backfilled_into_game_snapshot(tmp_path):
    database = Database(tmp_path / "direct-score-backfill.sqlite3")
    previous_version = 15
    async with database.connect() as connection:
        await connection.executescript(
            "\n".join(MIGRATIONS[:previous_version])
            + f"\nPRAGMA user_version = {previous_version};"
        )
        await connection.executemany(
            "INSERT INTO users (telegram_id, first_name, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 0)",
            [(101, "first", "first"), (202, "second", "second")],
        )
        await connection.executemany(
            "INSERT INTO user_stats (telegram_id) VALUES (?)", [(101,), (202,)]
        )
        cursor = await connection.execute(
            "INSERT INTO games (invite_token, creator_id, player2_id, fists, total_hands, "
            "game_type, created_at, updated_at) "
            "VALUES ('direct', 101, 202, 2, 3, 'truth_or_dare', 0, 0)"
        )
        game_id = cursor.lastrowid
        await connection.executemany(
            "INSERT INTO score_events "
            "(user_id, game_id, reason, hand_number, amount, created_at) "
            "VALUES (?, ?, 'challenge', ?, 1, 0)",
            [(101, game_id, 1), (202, game_id, 2), (202, game_id, 3)],
        )
        await connection.commit()

    await database.initialize()

    game = await GameService(database).get_game(game_id)
    assert (game.player1_score, game.player2_score) == (1, 2)


async def test_duplicate_legacy_score_created_after_ledger_is_removed(tmp_path):
    database = Database(tmp_path / "duplicate-legacy.sqlite3")
    old_version = 15
    async with database.connect() as connection:
        await connection.executescript(
            "\n".join(MIGRATIONS[:old_version]) + f"\nPRAGMA user_version = {old_version};"
        )
        await connection.executemany(
            "INSERT INTO users (telegram_id, first_name, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 0)",
            [(101, "first", "first"), (202, "second", "second")],
        )
        await connection.executemany(
            "INSERT INTO user_stats (telegram_id) VALUES (?)", [(101,), (202,)]
        )
        cursor = await connection.execute(
            "INSERT INTO games (invite_token, creator_id, player2_id, fists, total_hands, "
            "created_at, updated_at) VALUES ('scored', 101, 202, 2, 3, 0, 0)"
        )
        await connection.execute(
            "INSERT INTO score_events "
            "(user_id, game_id, reason, hand_number, amount, created_at) "
            "VALUES (101, ?, 'round', 1, 1, 1)",
            (cursor.lastrowid,),
        )
        await connection.commit()

    await database.initialize()

    async with database.connect() as connection:
        events = await (
            await connection.execute(
                "SELECT reason, amount FROM score_events WHERE user_id = 101 ORDER BY id"
            )
        ).fetchall()
        total = await (
            await connection.execute("SELECT points_won FROM user_stats WHERE telegram_id = 101")
        ).fetchone()
    assert [(row["reason"], row["amount"]) for row in events] == [("round", 1)]
    assert total["points_won"] == 1


async def test_old_tied_direct_match_is_changed_from_player_two_win_to_draw(tmp_path):
    database = Database(tmp_path / "direct-tie.sqlite3")
    old_version = 16
    async with database.connect() as connection:
        await connection.executescript(
            "\n".join(MIGRATIONS[:old_version]) + f"\nPRAGMA user_version = {old_version};"
        )
        await connection.executemany(
            "INSERT INTO users (telegram_id, first_name, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 0)",
            [(101, "first", "first"), (202, "second", "second")],
        )
        await connection.execute(
            "INSERT INTO user_stats (telegram_id, games_played, losses) VALUES (101, 1, 1)"
        )
        await connection.execute(
            "INSERT INTO user_stats (telegram_id, games_played, wins) VALUES (202, 1, 1)"
        )
        cursor = await connection.execute(
            "INSERT INTO games (invite_token, creator_id, player2_id, fists, total_hands, "
            "game_type, status, phase, player1_score, player2_score, winner_id, loser_id, "
            "created_at, updated_at) VALUES "
            "('tie', 101, 202, 2, 3, 'truth_or_dare', 'finished', 'finished', "
            "0, 0, 202, 101, 0, 0)"
        )
        game_id = cursor.lastrowid
        await connection.commit()

    await database.initialize()

    game = await GameService(database).get_game(game_id)
    assert game.winner_id is game.loser_id is None
    for user_id in (101, 202):
        stats = await GameService(database).get_stats(user_id)
        assert (stats.games_played, stats.wins, stats.losses) == (1, 0, 0)


async def test_database_rejects_answer_with_wrong_participants(database, service, players):
    a, b = [player.telegram_id for player in players]
    game = await service.create_game(a, 2, 3)
    question_id = await service.add_question("truth", "سؤال")
    with pytest.raises(aiosqlite.IntegrityError, match="participants"):
        async with database.transaction() as connection:
            await connection.execute(
                "INSERT INTO question_answers (game_id, question_id, respondent_id, opponent_id, "
                "question_text, asked_at) VALUES (?, ?, ?, ?, 'question', 0)",
                (game.id, question_id, a, b),
            )
