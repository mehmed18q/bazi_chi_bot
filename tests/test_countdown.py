from bazi_chi_bot.countdown import (
    FINAL_COUNTDOWN_TEXT,
    CountdownScheduler,
    CountdownService,
    countdown_message,
    format_remaining,
    humanize_remaining,
    meeting_time_text,
    next_delivery_at,
    parse_admin_ids,
    parse_target_datetime,
)
from bazi_chi_bot.models import CountdownStatus


def test_countdown_format_and_configuration_parsing():
    assert format_remaining(46 * 60 + 24) == "46:24"
    assert format_remaining(2 * 3600 + 46 * 60 + 24) == "02:46:24"
    assert format_remaining(59) == "00:59"
    assert humanize_remaining(2 * 3600 + 46 * 60 + 24) == "2 ساعت و 46 دقیقه و 24 ثانیه"
    assert countdown_message(59).startswith("⏳ <b>00:59</b> 🥲")
    target = int(parse_target_datetime("2026-09-08 08:30", "Asia/Tehran").timestamp())
    assert meeting_time_text(target, "Asia/Tehran") == ("سه‌شنبه ساعت ۸:۳۰ صبح، وعدهٔ دیدار ماست 💞")
    assert meeting_time_text(target, "Asia/Tehran") in countdown_message(59, target_at=target)
    assert parse_admin_ids("101, 202") == frozenset({101, 202})
    assert parse_target_datetime("2026-09-08 21:30", "Asia/Tehran").utcoffset() is not None


def test_next_delivery_hits_faster_cadence_boundaries():
    assert next_delivery_at(0, 3660) == 60
    assert next_delivery_at(0, 3600) == 600
    assert next_delivery_at(0, 601) == 1
    assert next_delivery_at(0, 600) == 300
    assert next_delivery_at(0, 61) == 1
    assert next_delivery_at(0, 60) == 10
    assert next_delivery_at(0, 5) == 5


async def test_countdown_is_persistent_and_replaces_previous_for_recipient(database, players):
    first, second = players
    service = CountdownService(database)

    original, replaced = await service.create(
        first.telegram_id, second.telegram_id, target_at=10_000, now=1_000
    )
    replacement, replaced = await service.create(
        first.telegram_id, second.telegram_id, target_at=20_000, now=2_000
    )

    assert replaced is True
    restarted_service = CountdownService(database)
    active = await restarted_service.active_created_by(first.telegram_id)
    assert active == [replacement]

    async with database.connect() as connection:
        row = await (
            await connection.execute("SELECT status FROM countdowns WHERE id = ?", (original.id,))
        ).fetchone()
    assert row["status"] == CountdownStatus.CANCELLED


async def test_scheduler_sends_countdown_then_completes(database, players):
    first, second = players
    service = CountdownService(database)
    countdown, _ = await service.create(
        first.telegram_id, second.telegram_id, target_at=1_120, now=1_000
    )

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))

    bot = FakeBot()
    scheduler = CountdownScheduler(service, bot)
    await scheduler._deliver(countdown, 1_000)

    active = await service.next_due()
    assert bot.messages[0][0] == second.telegram_id
    assert bot.messages[0][1].startswith("⏳ <b>02:00</b> 🥲")
    assert active is not None
    assert active.next_run_at == 1_060

    await scheduler._deliver(active, 1_120)
    assert bot.messages[-1] == (second.telegram_id, FINAL_COUNTDOWN_TEXT)
    assert await service.next_due() is None

    async with database.connect() as connection:
        row = await (
            await connection.execute(
                "SELECT status, completed_at FROM countdowns WHERE id = ?", (countdown.id,)
            )
        ).fetchone()
    assert row["status"] == CountdownStatus.COMPLETED
    assert row["completed_at"] == 1_120


async def test_active_countdowns_are_made_due_on_startup(database, players):
    first, second = players
    service = CountdownService(database)
    countdown, _ = await service.create(
        first.telegram_id, second.telegram_id, target_at=10_000, now=1_000
    )
    await service.record_sent(countdown.id, sent_at=1_000, target_at=countdown.target_at)

    queued = await service.make_all_active_due(now=1_500)
    due = await service.next_due()

    assert queued == 1
    assert due is not None
    assert due.id == countdown.id
    assert due.next_run_at == 1_500


async def test_only_saved_users_are_offered_as_countdown_targets(database, players):
    first, second = players
    service = CountdownService(database)

    users = await service.all_users()

    assert {user.telegram_id for user in users} == {first.telegram_id, second.telegram_id}
