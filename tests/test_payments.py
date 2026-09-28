import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.enums import ChatType
from aiogram.types import Chat, Message
from aiogram.types import User as TelegramUser

from bazi_chi_bot.game import GameService
from bazi_chi_bot.handlers import build_router
from bazi_chi_bot.models import PaymentReceiptStatus, User
from bazi_chi_bot.telegram.keyboards import admin_menu_keyboard
from bazi_chi_bot.telegram.payments import PaymentAccessMiddleware, activation_gate_text


async def test_users_get_stable_internal_ids_and_admin_can_change_payment_settings(
    database, service, players
):
    first, second = players
    assert (first.id, second.id) == (1, 2)

    await service.save_user(first)
    assert (await service.get_user(first.telegram_id)).id == 1

    defaults = await service.payment_settings()
    assert defaults.base_amount_toman == 100_000
    assert defaults.card_number == "6219861814466156"
    assert defaults.card_holder == "محمد صادق کیومرثی"
    assert await service.expected_payment_amount(first) == 100_001

    for position in range(3, 13):
        await service.save_user(
            User(10_000 + position, None, f"کاربر {position}", None, f"کاربر {position}")
        )
    twelfth = await service.get_user(10_012)
    assert twelfth is not None and twelfth.id == 12
    assert await service.expected_payment_amount(twelfth) == 100_012

    changed = await service.update_payment_settings(
        1767552952,
        base_amount_toman=250_000,
        card_number="6219-8618-1446-6156",
        card_holder="صاحب کارت جدید",
    )
    assert changed.base_amount_toman == 250_000
    assert changed.card_number == "6219861814466156"
    assert await GameService(database).expected_payment_amount(second) == 250_002


async def test_receipts_are_persisted_and_only_one_review_activates_user(
    database, service, players
):
    first = players[0]
    text_receipt = await service.submit_payment_receipt(
        first.telegram_id, receipt_text="کد پیگیری ۱۲۳"
    )
    photo_receipt = await service.submit_payment_receipt(
        first.telegram_id,
        receipt_text="تصویر فیش",
        telegram_file_id="telegram-photo-file-id",
    )
    assert text_receipt.expected_amount_toman == 100_000 + first.id
    assert photo_receipt.receipt_type == "photo"

    reviews = await asyncio.gather(
        service.review_payment_receipt(photo_receipt.id, 1767552952, True),
        service.review_payment_receipt(photo_receipt.id, 1767552952, True),
    )
    assert sum(changed for _, changed in reviews) == 1

    activated = await GameService(database).get_user(first.telegram_id)
    assert activated is not None and activated.is_activated
    assert activated.activation_approved_by == 1767552952
    assert (await service.payment_receipt(photo_receipt.id)).status is PaymentReceiptStatus.APPROVED
    assert (await service.payment_receipt(text_receipt.id)).status is PaymentReceiptStatus.REJECTED
    with pytest.raises(ValueError, match="already activated"):
        await service.submit_payment_receipt(first.telegram_id, receipt_text="تکراری")


async def test_start_shows_games_then_exact_activation_amount(service, players):
    first = players[0]
    router = build_router(service)
    handler = next(
        item.callback for item in router.message.handlers if item.callback.__name__ == "start"
    )
    message = SimpleNamespace(
        from_user=TelegramUser(
            id=first.telegram_id,
            is_bot=False,
            first_name=first.first_name,
            username=first.username,
        ),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(total_count=0, photos=[]))
    )
    state = SimpleNamespace(clear=AsyncMock())

    await handler(message, SimpleNamespace(args=None), bot, state)

    assert message.answer.await_count == 2
    assert "چهار بازی" in message.answer.await_args_list[0].args[0]
    payment_text = message.answer.await_args_list[1].args[0]
    assert "100,001 تومان" in payment_text
    assert "6219861814466156" in payment_text
    assert "محمد صادق کیومرثی" in payment_text


async def test_invitation_survives_payment_gate_and_resumes_after_activation(service, players):
    creator, guest = players
    game = await service.create_game(creator.telegram_id, 2, 3)
    router = build_router(service)
    handler = next(
        item.callback for item in router.message.handlers if item.callback.__name__ == "start"
    )
    telegram_guest = TelegramUser(
        id=guest.telegram_id,
        is_bot=False,
        first_name=guest.first_name,
        username=guest.username,
    )
    message = SimpleNamespace(from_user=telegram_guest, answer=AsyncMock())
    bot = SimpleNamespace(
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(total_count=0, photos=[])),
        send_message=AsyncMock(return_value=None),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await handler(message, SimpleNamespace(args=f"join_{game.invite_token}"), bot, state)
    gated = await service.get_user(guest.telegram_id)
    assert gated is not None and gated.pending_invite_token == game.invite_token
    assert (await service.get_game(game.id)).status.value == "waiting"
    assert (await service.get_stats(guest.telegram_id)).points_won == 0

    await service.set_user_activation(guest.telegram_id, 1767552952, True)
    await handler(message, SimpleNamespace(args=None), bot, state)

    joined = await service.get_game(game.id)
    assert joined.player2_id == guest.telegram_id
    activated = await service.get_user(guest.telegram_id)
    assert activated is not None and activated.pending_invite_token is None
    assert (await service.get_stats(guest.telegram_id)).points_won == 1


async def test_text_receipt_is_forwarded_to_reviewer_and_approval_notifies_user(service, players):
    first = players[0]
    reviewer = 1767552952
    router = build_router(
        service,
        countdown_admin_ids=frozenset({reviewer}),
        payment_reviewer_ids=frozenset({reviewer}),
    )
    submit_handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "payment_receipt_submit"
    )
    review_handler = next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "payment_review"
    )
    message = SimpleNamespace(
        from_user=TelegramUser(
            id=first.telegram_id,
            is_bot=False,
            first_name=first.first_name,
            username=first.username,
        ),
        text="واریز شد؛ پیگیری ۱۲۳",
        caption=None,
        photo=None,
        chat=SimpleNamespace(id=first.telegram_id),
        message_id=55,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())
    bot = SimpleNamespace(
        forward_message=AsyncMock(),
        send_message=AsyncMock(return_value=None),
    )

    await submit_handler(message, state, bot)

    receipt = await service.latest_pending_payment_receipt(first.telegram_id)
    assert receipt is not None
    bot.forward_message.assert_awaited_once_with(
        chat_id=reviewer,
        from_chat_id=first.telegram_id,
        message_id=55,
    )
    review_card = bot.send_message.await_args_list[0]
    assert review_card.args[0] == reviewer
    assert "شناسهٔ داخلی" in review_card.args[1]
    callbacks = {
        button.callback_data
        for row in review_card.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert f"payment:approve:{receipt.id}" in callbacks

    bot.send_message.reset_mock()
    callback = SimpleNamespace(
        from_user=TelegramUser(id=reviewer, is_bot=False, first_name="ادمین"),
        data=f"payment:approve:{receipt.id}",
        message=SimpleNamespace(edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )
    await review_handler(callback, bot)

    activated = await service.get_user(first.telegram_id)
    assert activated is not None and activated.is_activated
    activation_message = bot.send_message.await_args_list[0]
    assert activation_message.args[0] == first.telegram_id
    assert "ربات فعال شد" in activation_message.args[1]
    assert "نام نمایشی تو" in activation_message.args[1]
    activation_callbacks = {
        button.callback_data
        for row in activation_message.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert "profile:name:change" in activation_callbacks


async def test_activation_copy_and_admin_menu_include_payment_controls(service, players):
    text = await activation_gate_text(service, players[1])
    assert "100,002 تومان" in text
    callbacks = {
        button.callback_data for row in admin_menu_keyboard().inline_keyboard for button in row
    }
    assert "menu:payments" in callbacks


async def test_payment_middleware_blocks_until_receipt_is_approved(service, players):
    first = players[0]
    middleware = PaymentAccessMiddleware(service, frozenset({1767552952}))
    handler = AsyncMock(return_value="handled")
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=first.telegram_id, type=ChatType.PRIVATE),
        from_user=TelegramUser(
            id=first.telegram_id,
            is_bot=False,
            first_name=first.first_name,
        ),
        text="/stats",
    )

    with patch("bazi_chi_bot.telegram.payments.show_activation_gate", new_callable=AsyncMock) as gate:
        assert await middleware(handler, message, {}) is None
        gate.assert_awaited_once_with(message, service)
    handler.assert_not_awaited()

    receipt = await service.submit_payment_receipt(first.telegram_id, receipt_text="رسید")
    await service.review_payment_receipt(receipt.id, 1767552952, True)
    assert await middleware(handler, message, {}) == "handled"
    handler.assert_awaited_once()
