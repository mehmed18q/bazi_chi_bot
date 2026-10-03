import time
from datetime import date

import pytest

from bazi_chi_bot.daily_schedule import challenge_window, local_date, next_reminder_at
from bazi_chi_bot.errors import DailyChallengeClosed
from bazi_chi_bot.game import GameService
from bazi_chi_bot.models import GameStatus, GameType
from bazi_chi_bot.reset_database import reset_history
from bazi_chi_bot.services.countdowns import CountdownService
from bazi_chi_bot.services.daily_challenges import DAILY_TYPES
from bazi_chi_bot.services.solo import choose_code_guess, choose_tic_tac_toe_cell, choose_word_guess
from bazi_chi_bot.telegram.countdown_worker import CountdownScheduler
from bazi_chi_bot.telegram.daily_worker import DailyChallengeWorker


def _challenge_times() -> tuple[int, int]:
    return challenge_window(local_date(int(time.time())))


def test_same_daily_public_state_gives_same_robot_decision():
    assert choose_word_guess(5, (), "2026-09-30:1") == choose_word_guess(5, (), "2026-09-30:1")
    assert choose_code_guess((), "2026-09-30:1") == choose_code_guess((), "2026-09-30:1")
    assert choose_tic_tac_toe_cell(".........", "2026-09-30") == choose_tic_tac_toe_cell(
        ".........", "2026-09-30"
    )


def test_tehran_window_and_next_reminder_boundaries():
    start, end = challenge_window(date(2026, 9, 30))
    assert end - start == 3600
    assert local_date(start) == date(2026, 9, 30)
    assert next_reminder_at(start - 301) == start - 300
    assert next_reminder_at(start - 300) == challenge_window(date(2026, 10, 1))[0] - 300


async def test_daily_shared_config_one_attempt_for_free_users(service, players, monkeypatch):
    start, end = _challenge_times()
    monkeypatch.setattr("time.time", lambda: start + 30)
    monkeypatch.setattr(
        "bazi_chi_bot.services.daily_challenges.secrets.choice", lambda items: items[0]
    )
    monkeypatch.setattr("bazi_chi_bot.services.daily_challenges.secrets.randbelow", lambda n: 0)
    first, second = players
    challenge = await service.daily.ensure_today()
    assert challenge.game_type is GameType.GOL_YA_POOCH
    assert challenge.total_hands == 3 and challenge.fists == 2
    assert challenge.secrets == (1, 1, 1)
    assert await service.daily.ensure_today() == challenge

    first_game = await service.daily.start(first.telegram_id)
    second_game = await service.daily.start(second.telegram_id)
    assert first_game.id != second_game.id
    assert first_game.daily_challenge_date == second_game.daily_challenge_date
    assert (await service.daily.start(first.telegram_id)).id == first_game.id

    monkeypatch.setattr("time.time", lambda: end)
    with pytest.raises(DailyChallengeClosed):
        await service.hide_fist(first_game.id, first.telegram_id, 1, first_game.version)
    await service.daily.close_day(local_date(end - 1), now=end)
    assert (await service.get_game(first_game.id)).status is GameStatus.CANCELLED
    with pytest.raises(DailyChallengeClosed):
        await service.daily.start(first.telegram_id)


async def test_daily_notifications_are_personalized_and_idempotent(service, players, monkeypatch):
    start, end = _challenge_times()
    monkeypatch.setattr("time.time", lambda: start + 10)
    first, second = players
    await service.set_user_activation(first.telegram_id, 999, True)
    await service.set_user_activation(second.telegram_id, 999, True)
    challenge = await service.daily.ensure_today()
    await service.daily.start(first.telegram_id)
    await service.daily.queue_notifications(challenge, "start")
    await service.daily.queue_notifications(challenge, "start")
    sent = []
    for _ in range(2):
        item = await service.daily.next_notification()
        assert item is not None and item[2] == "start"
        sent.append(item[1])
        await service.daily.record_notification(*item)
    assert set(sent) == {first.telegram_id, second.telegram_id}
    assert await service.daily.next_notification() is None

    monkeypatch.setattr("time.time", lambda: end + 1)
    await service.daily.close_day(local_date(end - 1), now=end + 1)
    await service.daily.queue_notifications(challenge, "end")
    participated = await service.daily.notification_text(
        challenge.challenge_date, first.telegram_id, "end"
    )
    missed = await service.daily.notification_text(
        challenge.challenge_date, second.telegram_id, "end"
    )
    assert "شرکت کردی" in participated
    assert "شرکت نکردی" in missed


async def test_daily_reminder_repeats_without_replacing_admin_countdown(database, service, players):
    start, _ = _challenge_times()
    user = players[0]
    countdowns = CountdownService(database)
    reminder = await countdowns.subscribe_daily_reminder(user.telegram_id, now=start)
    assert reminder.kind == "daily_reminder"
    assert reminder.target_at == next_reminder_at(start)
    assert (
        await countdowns.subscribe_daily_reminder(user.telegram_id, now=start)
    ).id == reminder.id
    normal, _ = await countdowns.create(user.telegram_id, user.telegram_id, start + 3600, now=start)
    assert normal.kind == "standard"
    assert (await countdowns.daily_reminder(user.telegram_id)).id == reminder.id
    await countdowns.make_all_active_due(now=start + 5)
    assert (await countdowns.daily_reminder(user.telegram_id)).next_run_at == reminder.next_run_at
    await countdowns.record_daily_reminder_sent(reminder.id, reminder.target_at)
    moved = await countdowns.daily_reminder(user.telegram_id)
    assert moved is not None and moved.target_at > reminder.target_at
    assert await countdowns.cancel_daily_reminder(user.telegram_id)
    assert await countdowns.daily_reminder(user.telegram_id) is None
    assert (await countdowns.next_due()).id == normal.id


async def test_admin_exemption_applies_to_reminder_delivery_and_daily_game(
    database, players, monkeypatch
):
    start, _ = _challenge_times()
    admin_id = players[0].telegram_id
    admin_ids = frozenset({admin_id})
    countdowns = CountdownService(database, activation_exempt_ids=admin_ids)
    service = GameService(database, activation_exempt_ids=admin_ids)
    reminder = await countdowns.subscribe_daily_reminder(admin_id, now=start - 360)

    class BotStub:
        def __init__(self):
            self.messages = []

        async def send_message(self, user_id, text, **kwargs):
            self.messages.append((user_id, text))

    bot = BotStub()
    await CountdownScheduler(countdowns, bot)._deliver(reminder, reminder.target_at)
    assert bot.messages[0][0] == admin_id
    assert await countdowns.daily_reminder(admin_id) is not None

    monkeypatch.setattr("time.time", lambda: start + 5)
    challenge = await service.daily.ensure_today()
    assert (await service.daily.start(admin_id)).creator_id == admin_id
    await service.daily.queue_notifications(challenge, "start")
    assert await service.daily.next_notification() == (challenge.challenge_date, admin_id, "start")
    assert (await countdowns.subscribe_daily_reminder(players[1].telegram_id, now=start)).kind == "daily_reminder"


async def test_reminder_and_start_end_worker_delivery(database, service, players, monkeypatch):
    start, end = _challenge_times()
    user = players[0]
    await service.set_user_activation(user.telegram_id, 999, True)
    countdowns = CountdownService(database)
    reminder = await countdowns.subscribe_daily_reminder(user.telegram_id, now=start - 360)

    class BotStub:
        def __init__(self):
            self.messages = []

        async def send_message(self, user_id, text, **kwargs):
            self.messages.append((user_id, text))

    bot = BotStub()
    await CountdownScheduler(countdowns, bot)._deliver(reminder, reminder.target_at)
    assert "۵ دقیقه" in bot.messages[0][1]
    assert (await countdowns.daily_reminder(user.telegram_id)).target_at > reminder.target_at

    monkeypatch.setattr("time.time", lambda: start + 5)
    challenge = await service.daily.ensure_today()
    await service.daily.queue_notifications(challenge, "start")
    worker = DailyChallengeWorker(service.daily, bot)
    await worker._deliver_pending()
    assert any("شروع شد" in text for _, text in bot.messages)

    monkeypatch.setattr("time.time", lambda: end + 5)
    await service.daily.queue_notifications(challenge, "end")
    await worker._deliver_pending()
    assert any("تمام شد" in text for _, text in bot.messages)
    assert await service.daily.next_notification() is None


@pytest.mark.parametrize(
    "game_type", [GameType.GOL_YA_POOCH, GameType.WORD_GUESS, GameType.MASTERMIND]
)
async def test_daily_bot_secret_is_shared_between_players(service, players, monkeypatch, game_type):
    start, _ = _challenge_times()
    monkeypatch.setattr("time.time", lambda: start + 10)
    monkeypatch.setattr(
        "bazi_chi_bot.services.daily_challenges.secrets.choice",
        lambda options: game_type if options is DAILY_TYPES else options[0],
    )
    monkeypatch.setattr("bazi_chi_bot.services.daily_challenges.secrets.randbelow", lambda n: 1)
    for player in players:
        await service.set_user_activation(player.telegram_id, 999, True)
    first = (await service.start_daily_challenge(players[0].telegram_id)).game
    second = (await service.start_daily_challenge(players[1].telegram_id)).game
    assert first.game_type is second.game_type is game_type
    if game_type is GameType.GOL_YA_POOCH:
        assert first.hidden_fist == second.hidden_fist
    elif game_type is GameType.WORD_GUESS:
        assert first.word_secret == second.word_secret
    else:
        assert first.mastermind_secret == second.mastermind_secret


async def test_daily_win_gets_one_point_and_end_message(service, players, monkeypatch):
    start, end = _challenge_times()
    monkeypatch.setattr("time.time", lambda: start + 20)
    monkeypatch.setattr(
        "bazi_chi_bot.services.daily_challenges.secrets.choice", lambda options: options[0]
    )
    monkeypatch.setattr("bazi_chi_bot.services.daily_challenges.secrets.randbelow", lambda n: 0)
    monkeypatch.setattr(
        "bazi_chi_bot.services.solo._public_choice", lambda options, seed: options[0]
    )
    player = players[0]
    await service.set_user_activation(player.telegram_id, 999, True)
    game = (await service.start_daily_challenge(player.telegram_id)).game
    await service.hide_fist(game.id, player.telegram_id, 2, game.version)
    game = (await service.advance_bot(game.id)).game
    result = await service.guess_fist(game.id, player.telegram_id, 1, game.version)
    game = (await service.advance_bot(result.game.id)).game
    await service.hide_fist(game.id, player.telegram_id, 2, game.version)
    game = (await service.advance_bot(game.id)).game
    assert game.status is GameStatus.FINISHED
    assert game.winner_id == player.telegram_id
    assert (await service.get_stats(player.telegram_id)).points_won == 1
    monkeypatch.setattr("time.time", lambda: end + 1)
    text = await service.daily.notification_text(
        game.daily_challenge_date, player.telegram_id, "end"
    )
    assert "یک امتیاز گرفتی" in text


async def test_history_reset_clears_daily_configuration_and_notifications(
    database, service, players, monkeypatch
):
    start, _ = _challenge_times()
    monkeypatch.setattr("time.time", lambda: start + 5)
    await service.set_user_activation(players[0].telegram_id, 999, True)
    challenge = await service.daily.ensure_today()
    await service.daily.queue_notifications(challenge, "start")
    await service.daily.start(players[0].telegram_id)
    counts = await reset_history(database)
    assert counts["daily_challenges"] == 1
    assert counts["daily_notifications"] == 2
    assert await service.daily.get(local_date(start)) is None
