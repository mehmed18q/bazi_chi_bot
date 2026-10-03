from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import User as TelegramUser

from bazi_chi_bot.models import GameType, User
from bazi_chi_bot.telegram.groups import build_group_router
from bazi_chi_bot.telegram.router import build_router


async def test_referral_awards_each_point_once_after_payment(service, players):
    inviter, invitee = players
    token = await service.referrals.code_for(inviter.telegram_id)
    assert token == await service.referrals.code_for(inviter.telegram_id)
    assert await service.referrals.claim_code(invitee.telegram_id, token)
    assert not await service.referrals.claim_code(invitee.telegram_id, token)
    assert not await service.referrals.claim_code(inviter.telegram_id, token)
    assert (await service.get_stats(inviter.telegram_id)).points_won == 1
    assert (await service.referrals.stats(inviter.telegram_id)).remaining == 10

    receipt = await service.submit_payment_receipt(invitee.telegram_id, receipt_text="رسید")
    assert (await service.review_payment_receipt(receipt.id, 999, True))[1]
    assert not (await service.review_payment_receipt(receipt.id, 999, True))[1]
    await service.set_user_activation(invitee.telegram_id, 999, True)
    assert (await service.get_stats(inviter.telegram_id)).points_won == 2
    stats = await service.referrals.stats(inviter.telegram_id)
    assert (stats.invited, stats.activated, stats.remaining) == (1, 1, 9)
    notices, unlocked = await service.referrals.pending_notices(inviter.telegram_id)
    assert [notice.kind for notice in notices] == ["joined", "activated"]
    assert not unlocked
    await service.referrals.mark_notices(inviter.telegram_id, notices, False)
    assert (await service.referrals.pending_notices(inviter.telegram_id))[0] == []


async def test_referral_activation_cascades_and_admin_revoke_stays_revoked(service, players):
    first, second = players
    third = User(303, "third", "سوم", None, "سوم")
    fourth = User(404, "fourth", "چهارم", None, "چهارم")
    await service.save_user(third)
    await service.save_user(fourth)
    await service.referrals.set_required_activations(999, 1)
    assert await service.referrals.claim(second.telegram_id, first.telegram_id, "link")
    assert not await service.referrals.claim(first.telegram_id, second.telegram_id, "link")
    assert await service.referrals.claim(third.telegram_id, second.telegram_id, "link")
    await service.set_user_activation(third.telegram_id, 999, True)
    assert (await service.get_user(second.telegram_id)).is_activated
    assert (await service.get_user(first.telegram_id)).is_activated
    assert (await service.get_stats(first.telegram_id)).points_won == 2
    assert (await service.get_stats(second.telegram_id)).points_won == 2
    assert (await service.referrals.pending_notices(first.telegram_id))[1]

    await service.set_user_activation(first.telegram_id, 999, False)
    assert await service.referrals.claim(fourth.telegram_id, first.telegram_id, "link")
    await service.set_user_activation(fourth.telegram_id, 999, True)
    assert not (await service.get_user(first.telegram_id)).is_activated
    assert (await service.get_stats(first.telegram_id)).points_won == 4


async def test_lowering_target_unlocks_existing_eligible_referrers(service, players):
    inviter, invitee = players
    assert await service.referrals.claim(invitee.telegram_id, inviter.telegram_id, "link")
    await service.set_user_activation(invitee.telegram_id, 999, True)
    assert not (await service.get_user(inviter.telegram_id)).is_activated
    assert await service.referrals.set_required_activations(999, 1) == [inviter.telegram_id]
    assert (await service.get_user(inviter.telegram_id)).is_activated
    assert await service.referrals.set_required_activations(999, 1) == []


async def test_start_attributes_referral_links_game_and_group(service, players):
    inviter = players[0]
    await service.set_user_activation(inviter.telegram_id, 999, True)
    token = await service.referrals.code_for(inviter.telegram_id)
    game = await service.create_game(inviter.telegram_id, 2, 3, GameType.GOL_YA_POOCH)
    session = await service.group_games.create(-10011, 55, inviter.telegram_id, "صادق", GameType.WORD_GUESS)
    await service.referrals.set_group_owner(-10011, inviter.telegram_id)
    router = build_router(service)
    start = next(item.callback for item in router.message.handlers if item.callback.__name__ == "start")
    bot = SimpleNamespace(
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(total_count=0, photos=[]))
    )
    state = SimpleNamespace(clear=AsyncMock())
    for user_id, payload in (
        (303, f"ref_{token}"),
        (404, f"join_{game.invite_token}"),
        (505, f"group_{session.id}"),
        (606, "grpchat_-10011"),
    ):
        message = SimpleNamespace(
            from_user=TelegramUser(id=user_id, is_bot=False, first_name=f"user-{user_id}"),
            answer=AsyncMock(),
        )
        await start(message, SimpleNamespace(args=payload), bot, state)
    assert (await service.referrals.stats(inviter.telegram_id)).invited == 4
    assert (await service.get_stats(inviter.telegram_id)).points_won == 4


async def test_group_ready_button_records_free_member_as_referral(service, players):
    inviter = players[0]
    await service.set_user_activation(inviter.telegram_id, 999, True)
    session = await service.group_games.create(-10011, 55, inviter.telegram_id, "صادق", GameType.WORD_GUESS)
    handler = next(
        item.callback for item in build_group_router(service).callback_query.handlers
        if item.callback.__name__ == "group_join"
    )
    callback = SimpleNamespace(
        data=f"grp:join:{session.id}",
        from_user=TelegramUser(id=303, is_bot=False, first_name="سوم"),
        message=SimpleNamespace(chat=SimpleNamespace(id=-10011), message_id=55),
        answer=AsyncMock(),
    )
    await handler(callback, SimpleNamespace())
    assert (await service.referrals.stats(inviter.telegram_id)).invited == 1
    assert "اشتراک ویژه" in callback.answer.call_args.args[0]


async def test_adding_bot_to_group_requires_premium_and_remembers_inviter(service, players):
    handler = next(
        item.callback for item in build_group_router(service).my_chat_member.handlers
        if item.callback.__name__ == "bot_added_to_group"
    )
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-10011, type=ChatType.SUPERGROUP),
        from_user=TelegramUser(id=players[0].telegram_id, is_bot=False, first_name="صادق"),
        old_chat_member=SimpleNamespace(status=ChatMemberStatus.LEFT),
        new_chat_member=SimpleNamespace(status=ChatMemberStatus.MEMBER),
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(), leave_chat=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot")),
    )
    await handler(event, bot)
    bot.leave_chat.assert_awaited_once_with(-10011)
    assert await service.referrals.group_owner(-10011) is None

    bot.leave_chat.reset_mock()
    await service.set_user_activation(players[0].telegram_id, 999, True)
    await handler(event, bot)
    bot.leave_chat.assert_not_awaited()
    assert await service.referrals.group_owner(-10011) == players[0].telegram_id
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert "grpchat_-10011" in markup.inline_keyboard[0][0].url


async def test_start_reports_referral_points_and_remaining_target(service, players):
    inviter, invitee = players
    assert await service.referrals.claim(invitee.telegram_id, inviter.telegram_id, "link")
    await service.set_user_activation(invitee.telegram_id, 999, True)
    start = next(
        item.callback for item in build_router(service).message.handlers
        if item.callback.__name__ == "start"
    )
    message = SimpleNamespace(
        from_user=TelegramUser(id=inviter.telegram_id, is_bot=False, first_name=inviter.first_name),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(total_count=0, photos=[]))
    )
    await start(message, SimpleNamespace(args=None), bot, SimpleNamespace(clear=AsyncMock()))
    texts = [call.args[0] for call in message.answer.await_args_list]
    assert any("برای دعوت" in text and "فعال‌شدن" in text for text in texts)
    assert any("از <b>10</b>" in text and "<b>9</b>" in text for text in texts)
    assert (await service.referrals.pending_notices(inviter.telegram_id))[0] == []
