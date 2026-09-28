from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import User as TelegramUser

from bazi_chi_bot.handlers import build_router


async def test_admin_can_choose_kind_and_add_question(service, players):
    router = build_router(service, countdown_admin_ids=frozenset({players[0].telegram_id}))
    handlers = {handler.callback.__name__: handler.callback for handler in router.callback_query.handlers}
    message_handlers = {handler.callback.__name__: handler.callback for handler in router.message.handlers}
    callback = SimpleNamespace(
        from_user=TelegramUser(id=players[0].telegram_id, is_bot=False, first_name="ادمین"),
        data="menu:add_question", message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(),
    )
    state = SimpleNamespace(
        set_state=AsyncMock(), update_data=AsyncMock(), clear=AsyncMock(), get_data=AsyncMock(return_value={"question_kind": "truth"}),
    )
    await handlers["add_question_menu"](callback, state)
    assert state.set_state.await_count == 1
    callback.data = "admin_question:truth"
    await handlers["question_kind_selected"](callback, state)
    message = SimpleNamespace(
        from_user=TelegramUser(id=players[0].telegram_id, is_bot=False, first_name="ادمین"),
        text="سؤال اختصاصی ادمین؟", answer=AsyncMock(),
    )
    await message_handlers["question_text_received"](message, state)
    async with service.database.connect() as connection:
        row = await (await connection.execute(
            "SELECT kind, text, active FROM questions WHERE text = ?", (message.text,)
        )).fetchone()
    assert tuple(row) == ("truth", message.text, 1)


async def test_regular_user_cannot_open_question_admin_menu(service, players):
    router = build_router(service, countdown_admin_ids=frozenset({players[0].telegram_id}))
    handler = next(h.callback for h in router.callback_query.handlers if h.callback.__name__ == "add_question_menu")
    callback = SimpleNamespace(
        from_user=TelegramUser(id=players[1].telegram_id, is_bot=False, first_name="کاربر"),
        data="menu:add_question", message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(),
    )
    state = SimpleNamespace(set_state=AsyncMock())
    await handler(callback, state)
    state.set_state.assert_not_awaited()
    callback.answer.assert_awaited_once()
