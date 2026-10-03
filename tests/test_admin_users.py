from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import User as TelegramUser

from bazi_chi_bot.handlers import build_router


async def test_admin_profile_photo_and_details_are_sent_as_one_card(service, players):
    admin = players[0]
    await service.set_user_profile_photo(admin.telegram_id, "stored-photo-id")
    router = build_router(service, countdown_admin_ids=frozenset({admin.telegram_id}))
    handler = next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "admin_user_profile"
    )
    message = SimpleNamespace(
        delete=AsyncMock(),
        answer_photo=AsyncMock(),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )
    callback = SimpleNamespace(
        from_user=TelegramUser(
            id=admin.telegram_id,
            is_bot=False,
            first_name=admin.first_name,
            username=admin.username,
        ),
        data=f"admin_user:view:{admin.telegram_id}",
        message=message,
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(get_user_profile_photos=AsyncMock())

    await handler(callback, bot)

    message.delete.assert_awaited_once()
    message.answer_photo.assert_awaited_once()
    assert message.answer_photo.call_args.args[0] == "stored-photo-id"
    caption = message.answer_photo.call_args.kwargs["caption"]
    assert "پروندهٔ کاربر" in caption
    assert admin.display_name in caption
    assert "امتیاز" in caption
    callbacks = {
        button.callback_data
        for row in message.answer_photo.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert f"admin_user:activation:{admin.telegram_id}:on" in callbacks
    message.edit_text.assert_not_awaited()
    bot.get_user_profile_photos.assert_not_awaited()


async def test_admin_can_activate_and_deactivate_user_from_profile(service, players):
    admin, target = players
    router = build_router(service, countdown_admin_ids=frozenset({admin.telegram_id}))
    handler = next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "admin_user_activation"
    )
    message = SimpleNamespace(
        photo=None,
        edit_text=AsyncMock(),
    )
    callback = SimpleNamespace(
        from_user=TelegramUser(
            id=admin.telegram_id,
            is_bot=False,
            first_name=admin.first_name,
        ),
        data=f"admin_user:activation:{target.telegram_id}:on",
        message=message,
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock(return_value=None))

    await handler(callback, bot)

    activated = await service.get_user(target.telegram_id)
    assert activated is not None and activated.is_activated
    assert activated.activation_approved_by == admin.telegram_id
    assert "فعال ✅" in message.edit_text.call_args.args[0]
    callbacks = {
        button.callback_data
        for row in message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert f"admin_user:activation:{target.telegram_id}:off" in callbacks
    assert "اشتراک ویژهٔ بدون انقضای تو توسط ادمین فعال شد" in bot.send_message.call_args.args[1]
    assert "نام نمایشی تو" in bot.send_message.call_args.args[1]

    callback.data = f"admin_user:activation:{target.telegram_id}:off"
    message.edit_text.reset_mock()
    bot.send_message.reset_mock()
    await handler(callback, bot)

    deactivated = await service.get_user(target.telegram_id)
    assert deactivated is not None and not deactivated.is_activated
    assert deactivated.activation_approved_at is None
    assert "ندارد 🔒" in message.edit_text.call_args.args[0]
    assert "غیرفعال شد" in bot.send_message.call_args.args[1]
