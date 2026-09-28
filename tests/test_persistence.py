import asyncio

from bazi_chi_bot.db import MIGRATIONS
from bazi_chi_bot.game import GameService
from bazi_chi_bot.models import FinalChoice, GamePhase, GameStatus


async def test_schema_migration_is_versioned_and_repeatable(database):
    await database.initialize()
    async with database.connect() as connection:
        row = await (await connection.execute("PRAGMA user_version")).fetchone()
        tables = await (
            await connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ).fetchall()

    assert row[0] == len(MIGRATIONS)
    table_names = {row[0] for row in tables}
    assert {
        "users",
        "user_stats",
        "games",
        "countdowns",
        "payment_settings",
        "payment_receipts",
    } <= table_names


async def test_initialize_does_not_reapply_wal_while_another_reader_is_open(database):
    async with database.connect() as reader:
        await reader.execute("BEGIN")
        await reader.execute("SELECT count(*) FROM users")
        await asyncio.wait_for(database.initialize(), timeout=1)


async def test_new_service_instance_recovers_in_progress_game(database, service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 6, 5)
    game = await service.join_game(game.invite_token, second.telegram_id)
    game = await service.hide_fist(game.id, first.telegram_id, 5, game.version)

    restarted_service = GameService(database)
    recovered = await restarted_service.get_game(game.id)
    active = await restarted_service.active_games(second.telegram_id)

    assert recovered.phase is GamePhase.GUESSING
    assert recovered.hidden_fist == 5
    assert recovered.version == game.version
    assert [item.id for item in active] == [game.id]


async def test_profile_photo_file_id_is_persisted(database, service, players):
    user = players[0]
    assert (await service.get_user(user.telegram_id)).profile_photo_file_id is None

    await service.set_user_profile_photo(user.telegram_id, "telegram-photo-file-id")

    recovered = await GameService(database).get_user(user.telegram_id)
    assert recovered is not None
    assert recovered.profile_photo_file_id == "telegram-photo-file-id"


async def test_game_message_is_persisted_and_updated(service, players):
    game = await service.create_game(players[0].telegram_id, 2, 3)

    assert await service.game_message(game.id, players[0].telegram_id) is None
    await service.save_game_message(game.id, players[0].telegram_id, 101, 501)
    assert await service.game_message(game.id, players[0].telegram_id) == (101, 501)

    await service.save_game_message(game.id, players[0].telegram_id, 101, 502)
    assert await service.game_message(game.id, players[0].telegram_id) == (101, 502)


async def test_latest_finished_game_recovers_final_choice(database, service, players):
    first, second = players
    game = await service.create_game(first.telegram_id, 2, 3)
    game = await service.join_game(game.invite_token, second.telegram_id)

    for _ in range(3):
        game = await service.hide_fist(game.id, game.hider_id, 1, game.version)
        result = await service.guess_fist(game.id, game.guesser_id, 1, game.version)
        game = result.game

    assert game.status is GameStatus.CHOICE
    game = await service.choose_final(game.id, game.loser_id, FinalChoice.DARE, game.version)
    assert game.final_prompt_text == "یک جرأت کوتاه انجام بده."
    game = await service.submit_final_response(game.id, game.loser_id, "انجام شد.")

    restarted_service = GameService(database)
    recovered = await restarted_service.latest_finished_game(game.winner_id)

    assert recovered is not None
    assert recovered.id == game.id
    assert recovered.final_choice is FinalChoice.DARE
    assert recovered.final_prompt_text == "یک جرأت کوتاه انجام بده."
    assert recovered.final_response_text == "انجام شد."
