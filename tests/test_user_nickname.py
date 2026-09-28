from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TelegramUser

from bazi_chi_bot.game import GameService
from bazi_chi_bot.handlers import build_router
from bazi_chi_bot.models import User
from bazi_chi_bot.telegram.shared import ProfileSetup, game_names


async def test_nickname_follows_telegram_until_user_customizes_it(database, service):
    telegram_id = 909
    await service.save_user(User(telegram_id, "user", "نام", "قدیمی", "نام قدیمی"))
    initial = await service.get_user(telegram_id)
    assert initial is not None
    assert initial.display_name == initial.nickname == "نام قدیمی"
    assert initial.telegram_display_name == "نام قدیمی"
    assert not initial.nickname_is_custom

    await service.save_user(User(telegram_id, "user", "نام", "جدید", "نام جدید"))
    followed = await service.get_user(telegram_id)
    assert followed is not None and followed.display_name == "نام جدید"

    customized = await service.set_user_nickname(telegram_id, "  اسم   داخل ربات  ")
    assert customized is not None
    assert customized.display_name == customized.nickname == "اسم داخل ربات"
    assert customized.nickname_is_custom

    await service.save_user(User(telegram_id, "renamed", "تلگرام", "تازه", "تلگرام تازه"))
    persisted = await GameService(database).get_user(telegram_id)
    assert persisted is not None
    assert persisted.display_name == "اسم داخل ربات"
    assert persisted.telegram_display_name == "تلگرام تازه"
    assert persisted.nickname_is_custom

    await service.save_user(persisted)
    resaved = await service.get_user(telegram_id)
    assert resaved is not None and resaved.telegram_display_name == "تلگرام تازه"
    assert resaved.display_name == "اسم داخل ربات"

    reset = await service.set_user_nickname(telegram_id, None)
    assert reset is not None
    assert reset.display_name == "تلگرام تازه"
    assert reset.telegram_display_name == "تلگرام تازه"
    assert not reset.nickname_is_custom


async def test_nickname_validation_and_game_names(service, players):
    first, second = players
    with pytest.raises(ValueError, match="between 1 and 40"):
        await service.set_user_nickname(first.telegram_id, "   ")
    with pytest.raises(ValueError, match="between 1 and 40"):
        await service.set_user_nickname(first.telegram_id, "x" * 41)

    await service.set_user_nickname(first.telegram_id, "بازیکن ویژه")
    game = await service.create_game(first.telegram_id, 2, 3)
    game = await service.join_game(game.invite_token, second.telegram_id)
    names = await game_names(service, game)
    assert names[first.telegram_id] == "بازیکن ویژه"


async def test_user_can_change_and_reset_name_from_profile(service, players):
    user = players[0]
    await service.set_user_activation(user.telegram_id, 1767552952, True)
    router = build_router(service)
    callback_handlers = {
        item.callback.__name__: item.callback for item in router.callback_query.handlers
    }
    message_handlers = {item.callback.__name__: item.callback for item in router.message.handlers}
    telegram_account = TelegramUser(
        id=user.telegram_id,
        is_bot=False,
        first_name="نام تلگرام",
        username=user.username,
    )
    callback_message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(
        from_user=telegram_account,
        data="menu:profile",
        message=callback_message,
        answer=AsyncMock(),
    )

    await callback_handlers["menu_profile"](callback)
    profile_card = callback_message.edit_text.call_args.args[0]
    assert "نام داخل ربات" in profile_card
    callbacks = {
        button.callback_data
        for row in callback_message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert "profile:name:change" in callbacks

    state = SimpleNamespace(set_state=AsyncMock(), clear=AsyncMock())
    await callback_handlers["profile_name_change"](callback, state)
    state.set_state.assert_awaited_once_with(ProfileSetup.waiting_for_name)

    name_message = SimpleNamespace(
        from_user=telegram_account,
        text="  اسم   دلخواه  ",
        answer=AsyncMock(),
    )
    await message_handlers["profile_name_save"](name_message, state)
    saved = await service.get_user(user.telegram_id)
    assert saved is not None and saved.display_name == "اسم دلخواه"
    assert saved.nickname_is_custom

    callback.data = "profile:name:reset"
    callback_message.edit_text.reset_mock()
    await callback_handlers["profile_name_reset"](callback)
    reset = await service.get_user(user.telegram_id)
    assert reset is not None and reset.display_name == "نام تلگرام"
    assert not reset.nickname_is_custom
