import time

from bazi_chi_bot.daily_schedule import challenge_window, local_date
from bazi_chi_bot.db import MIGRATIONS, Database
from bazi_chi_bot.game import GameService
from bazi_chi_bot.models import GameType
from bazi_chi_bot.reset_database import reset_history
from bazi_chi_bot.rules import normalize_word
from bazi_chi_bot.services.daily_challenges import DAILY_TYPES
from bazi_chi_bot.word_bank import BOT_WORDS


def test_curated_words_are_distinct_and_valid():
    assert len(BOT_WORDS) >= 500
    assert len(BOT_WORDS) == len({normalize_word(word) for word in BOT_WORDS})
    assert {"میم", "وایب", "کراش", "پیتزا", "وروجک"} <= set(BOT_WORDS)


async def test_bot_never_reuses_word_for_same_player_across_games_and_restart(
    service, players, database
):
    player = players[0]
    seen = set()
    for index in range(3):
        current = service if index < 2 else GameService(database)
        game = await current.matches.create_solo_game(
            player.telegram_id, 2, 3, GameType.WORD_GUESS, bot_starts=True
        )
        game = await current.matches.choose_bot_word(game.id, "آب")
        assert game.word_secret not in seen
        seen.add(game.word_secret)
    assert "آب" in seen and len(seen) == 3
    async with database.connect() as connection:
        rows = await (
            await connection.execute(
                "SELECT word FROM bot_word_assignments WHERE user_id = ?",
                (player.telegram_id,),
            )
        ).fetchall()
    assert {row["word"] for row in rows} == seen


async def test_other_players_words_feed_bot_pool_but_own_words_do_not(
    service, players, monkeypatch, database
):
    first, second = players
    for author, opponent, word in (
        (first, second, "ابرگربه"),
        (second, first, "میمدونی"),
    ):
        game = await service.create_game(author.telegram_id, 2, 3, GameType.WORD_GUESS)
        game = await service.join_game(game.invite_token, opponent.telegram_id)
        await service.choose_word(game.id, author.telegram_id, word)

    monkeypatch.setattr("bazi_chi_bot.services.word_bank.secrets.randbelow", lambda n: 1)
    monkeypatch.setattr("bazi_chi_bot.services.word_bank.secrets.choice", lambda words: words[0])
    first_game = await service.matches.create_solo_game(
        first.telegram_id, 2, 3, GameType.WORD_GUESS, bot_starts=True
    )
    first_game = await service.matches.choose_bot_word(first_game.id)
    assert first_game.word_secret == "میمدونی"
    next_game = await service.matches.create_solo_game(
        first.telegram_id, 2, 3, GameType.WORD_GUESS, bot_starts=True
    )
    next_game = await service.matches.choose_bot_word(next_game.id)
    assert next_game.word_secret != first_game.word_secret

    second_game = await service.matches.create_solo_game(
        second.telegram_id, 2, 3, GameType.WORD_GUESS, bot_starts=True
    )
    second_game = await service.matches.choose_bot_word(second_game.id)
    assert second_game.word_secret == "ابرگربه"
    async with database.connect() as connection:
        count = await (
            await connection.execute("SELECT count(*) FROM human_word_submissions")
        ).fetchone()
    assert count[0] == 2


async def test_group_word_secret_joins_shared_human_pool(service, players, database):
    group = service.group_games
    first, second = players
    session = await group.create(
        -12345, 500, first.telegram_id, first.display_name, GameType.WORD_GUESS
    )
    session = await group.join(session.id, second.telegram_id, second.display_name)
    session = await group.start(session.id, first.telegram_id)
    await group.text_input(session.id, first.telegram_id, "word", "شوخکده")
    async with database.connect() as connection:
        row = await (
            await connection.execute(
                "SELECT user_id FROM human_word_submissions WHERE word = 'شوخکده'"
            )
        ).fetchone()
    assert row["user_id"] == first.telegram_id


async def test_daily_uses_fresh_word_and_reports_actual_choice(service, players, monkeypatch):
    player = players[0]
    await service.set_user_activation(player.telegram_id, 999, True)
    normal = await service.matches.create_solo_game(
        player.telegram_id, 2, 3, GameType.WORD_GUESS, bot_starts=True
    )
    normal = await service.matches.choose_bot_word(normal.id, BOT_WORDS[0])
    assert normal.word_secret == BOT_WORDS[0]

    start, end = challenge_window(local_date(int(time.time())))
    monkeypatch.setattr("time.time", lambda: start + 10)
    monkeypatch.setattr(
        "bazi_chi_bot.services.daily_challenges.secrets.choice",
        lambda options: GameType.WORD_GUESS if options is DAILY_TYPES else options[0],
    )
    monkeypatch.setattr("bazi_chi_bot.services.daily_challenges.secrets.randbelow", lambda n: 1)
    daily = (await service.start_daily_challenge(player.telegram_id)).game
    assert daily.word_secret != normal.word_secret
    monkeypatch.setattr("time.time", lambda: end + 1)
    message = await service.daily.notification_text(
        daily.daily_challenge_date, player.telegram_id, "end"
    )
    assert daily.word_secret in message
    assert BOT_WORDS[0] not in message


async def test_daily_can_use_another_players_word(service, players, monkeypatch):
    first, second = players
    game = await service.create_game(second.telegram_id, 2, 3, GameType.WORD_GUESS)
    game = await service.join_game(game.invite_token, first.telegram_id)
    await service.choose_word(game.id, second.telegram_id, "میمدونی")
    await service.set_user_activation(first.telegram_id, 999, True)
    start, _ = challenge_window(local_date(int(time.time())))
    monkeypatch.setattr("time.time", lambda: start + 10)
    monkeypatch.setattr(
        "bazi_chi_bot.services.daily_challenges.secrets.choice",
        lambda options: GameType.WORD_GUESS if options is DAILY_TYPES else options[0],
    )
    monkeypatch.setattr("bazi_chi_bot.services.daily_challenges.secrets.randbelow", lambda n: 1)
    daily = (await service.start_daily_challenge(first.telegram_id)).game
    assert daily.word_secret == "میمدونی"


async def test_word_bank_grows_past_curated_pool_without_repeating(
    service, players, monkeypatch
):
    monkeypatch.setattr("bazi_chi_bot.services.word_bank.BOT_WORDS", ("آب", "گل"))
    player = players[0]
    chosen = []
    for preferred in ("آب", "گل", None):
        game = await service.matches.create_solo_game(
            player.telegram_id, 2, 3, GameType.WORD_GUESS, bot_starts=True
        )
        game = await service.matches.choose_bot_word(game.id, preferred)
        chosen.append(game.word_secret)
    assert len(set(chosen)) == 3
    assert chosen[:2] == ["آب", "گل"]
    assert normalize_word(chosen[2]) == chosen[2]


async def test_history_reset_preserves_human_words_and_clears_bot_exposure(
    service, players, database
):
    first, second = players
    duo = await service.create_game(first.telegram_id, 2, 3, GameType.WORD_GUESS)
    duo = await service.join_game(duo.invite_token, second.telegram_id)
    await service.choose_word(duo.id, first.telegram_id, "بامزکده")
    solo = await service.matches.create_solo_game(
        second.telegram_id, 2, 3, GameType.WORD_GUESS, bot_starts=True
    )
    await service.matches.choose_bot_word(solo.id, "آب")
    counts = await reset_history(database)
    assert counts["bot_word_assignments"] == 1
    async with database.connect() as connection:
        human = await (
            await connection.execute("SELECT word FROM human_word_submissions")
        ).fetchall()
        bot = await (
            await connection.execute("SELECT count(*) FROM bot_word_assignments")
        ).fetchone()
    assert [row["word"] for row in human] == ["بامزکده"]
    assert bot[0] == 0


async def test_upgrade_collects_words_still_present_in_older_games(tmp_path):
    database = Database(tmp_path / "previous-schema.sqlite3")
    previous_version = len(MIGRATIONS) - 1
    async with database.connect() as connection:
        await connection.executescript(
            "\n".join(MIGRATIONS[:previous_version])
            + f"\nPRAGMA user_version = {previous_version};"
        )
        for user_id, name in ((101, "اول"), (202, "دوم"), (-1, "ربات")):
            await connection.execute(
                """INSERT INTO users
                   (telegram_id, first_name, display_name, created_at, updated_at)
                   VALUES (?, ?, ?, 0, 0)""",
                (user_id, name, name),
            )
        for token, hider_id, player2_id, is_solo, word in (
            ("human", 202, 202, 0, "شوخکده"),
            ("bot", -1, -1, 1, "آب"),
        ):
            await connection.execute(
                """INSERT INTO games
                   (invite_token, creator_id, player2_id, fists, total_hands,
                    game_type, is_solo, status, phase, hider_id, word_secret,
                    created_at, updated_at)
                   VALUES (?, 101, ?, 2, 3, 'word_guess', ?, 'active',
                           'guessing', ?, ?, 0, 0)""",
                (token, player2_id, is_solo, hider_id, word),
            )
        await connection.commit()
    await database.initialize()
    async with database.connect() as connection:
        human = await (
            await connection.execute("SELECT word, user_id FROM human_word_submissions")
        ).fetchall()
        bot = await (
            await connection.execute("SELECT word, user_id FROM bot_word_assignments")
        ).fetchall()
    assert [(row["word"], row["user_id"]) for row in human] == [("شوخکده", 202)]
    assert [(row["word"], row["user_id"]) for row in bot] == [("آب", 101)]
    service = GameService(database)
    game = await service.matches.create_solo_game(101, 2, 3, GameType.WORD_GUESS, bot_starts=True)
    game = await service.matches.choose_bot_word(game.id, "آب")
    assert game.word_secret != "آب"
