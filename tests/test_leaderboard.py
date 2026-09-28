import time

from bazi_chi_bot.leaderboard import leaderboard_period_start_epoch
from bazi_chi_bot.models import User
from bazi_chi_bot.ui import leaderboard_text


async def test_leaderboard_returns_top_three_and_current_position(service, players):
    first, second = players
    third = User(303, "third", "سوم", None, "سوم")
    fourth = User(404, "fourth", "چهارم", None, "چهارم")
    await service.save_user(third)
    await service.save_user(fourth)

    async with service.database.transaction() as connection:
        await connection.executemany(
            "INSERT INTO score_events (user_id, reason, amount, created_at) "
            "VALUES (?, 'legacy', ?, ?)",
            [
                (first.telegram_id, 2, int(time.time())),
                (second.telegram_id, 8, int(time.time())),
                (third.telegram_id, 5, int(time.time())),
                (fourth.telegram_id, 1, int(time.time())),
            ],
        )

    top = await service.leaderboard()
    current = await service.leaderboard_position(fourth.telegram_id)

    assert [entry.telegram_id for entry in top] == [
        second.telegram_id,
        third.telegram_id,
        first.telegram_id,
    ]
    assert [entry.rank for entry in top] == [1, 2, 3]
    assert current is not None
    assert current.rank == 4
    assert "چهارم" in leaderboard_text(top, current)


async def test_login_bonus_is_idempotent_monthly_and_all_time_keeps_history(service, players):
    first = players[0]
    await service.register_user_entry(first)
    assert (await service.get_stats(first.telegram_id)).points_won == 0

    await service.set_user_activation(first.telegram_id, 1767552952, True)
    await service.register_user_entry(first)
    await service.register_user_entry(first)

    period_start = leaderboard_period_start_epoch()
    async with service.database.transaction() as connection:
        await connection.execute(
            "INSERT INTO score_events (user_id, reason, amount, created_at) "
            "VALUES (?, 'legacy', 1, ?)",
            (first.telegram_id, period_start - 86400),
        )
        login_events = await (
            await connection.execute(
                "SELECT count(*) FROM score_events WHERE user_id = ? AND reason = 'login'",
                (first.telegram_id,),
            )
        ).fetchone()

    assert login_events[0] == 1
    assert (await service.get_stats(first.telegram_id)).points_won == 1
    all_time = await service.all_time_leaderboard()
    assert all_time[0].telegram_id == first.telegram_id
    assert all_time[0].points_won == 2


async def test_leaderboards_use_custom_nickname(service, players):
    first = players[0]
    await service.set_user_nickname(first.telegram_id, "قهرمان ماه")

    monthly = await service.leaderboard_position(first.telegram_id)
    all_time = await service.all_time_leaderboard_position(first.telegram_id)

    assert monthly is not None and monthly.display_name == "قهرمان ماه"
    assert all_time is not None and all_time.display_name == "قهرمان ماه"
