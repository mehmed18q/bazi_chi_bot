from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Message, User as TelegramUser

from bazi_chi_bot.persistence.sponsors import Sponsor
from bazi_chi_bot.telegram.keyboards import required_sponsors_keyboard
from bazi_chi_bot.telegram.sponsors import (
    SponsorMembershipMiddleware,
    deactivate_invalid_sponsors,
    check_sponsors,
    is_active_member,
    sponsor_gate_text,
)


def sponsor(
    sponsor_id: int,
    kind: str,
    title: str,
    *,
    chat_id: str | None = None,
) -> Sponsor:
    username = "partner_bot" if kind == "bot" else None
    return Sponsor(
        id=sponsor_id,
        kind=kind,
        title=title,
        chat_id=chat_id,
        username=username,
        url=f"https://t.me/{username or 'channel'}",
        active=True,
    )


def test_membership_statuses_include_restricted_members():
    assert is_active_member(SimpleNamespace(status=ChatMemberStatus.MEMBER))
    assert is_active_member(SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR))
    assert is_active_member(
        SimpleNamespace(status=ChatMemberStatus.RESTRICTED, is_member=True)
    )
    assert not is_active_member(
        SimpleNamespace(status=ChatMemberStatus.RESTRICTED, is_member=False)
    )
    assert not is_active_member(SimpleNamespace(status=ChatMemberStatus.LEFT))


async def test_check_sponsors_checks_channels_but_keeps_bot_links_visible():
    channel = sponsor(1, "channel", "ایران گره", chat_id="-100123")
    partner_bot = sponsor(2, "bot", "فروشگاه تایگو")
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.MEMBER)
        )
    )

    result = await check_sponsors(bot, 101, [channel, partner_bot])

    assert result.allowed
    assert result.sponsors == (channel, partner_bot)
    bot.get_chat_member.assert_awaited_once_with("-100123", 101)

    keyboard = required_sponsors_keyboard(list(result.sponsors), "join_abc")
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "📣 ورود به ایران گره" in labels
    assert "🤖 ورود به فروشگاه تایگو" in labels
    assert "sponsors:check:join_abc" in callbacks


async def test_check_sponsors_distinguishes_missing_from_unavailable():
    first = sponsor(1, "channel", "عضو نیست", chat_id="-1001")
    second = sponsor(2, "channel", "دسترسی ندارد", chat_id="-1002")
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            side_effect=[
                SimpleNamespace(status=ChatMemberStatus.LEFT),
                TelegramBadRequest(
                    method=SimpleNamespace(), message="member list is inaccessible"
                ),
            ]
        )
    )

    result = await check_sponsors(bot, 101, [first, second])

    assert result.missing_channels == (first,)
    assert result.unavailable_channels == (second,)
    assert not result.allowed


def test_unavailable_sponsor_title_is_html_escaped():
    unsafe = sponsor(1, "channel", "<کانال>", chat_id="-1001")
    check = SimpleNamespace(unavailable_channels=(unsafe,))

    text = sponsor_gate_text(check)

    assert "&lt;کانال&gt;" in text
    assert "<کانال>" not in text


async def test_admin_bypasses_membership_middleware():
    service = SimpleNamespace(active_sponsors=AsyncMock())
    middleware = SponsorMembershipMiddleware(service, frozenset({101}))
    handler = AsyncMock(return_value="handled")
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=101, type=ChatType.PRIVATE),
        from_user=TelegramUser(id=101, is_bot=False, first_name="مدیر"),
        text="آمار",
    )

    result = await middleware(handler, message, {"bot": SimpleNamespace()})

    assert result == "handled"
    handler.assert_awaited_once()
    service.active_sponsors.assert_not_awaited()


async def test_startup_deactivates_inaccessible_channel_but_not_bot():
    channel = sponsor(1, "channel", "ایران گره", chat_id="@iran")
    partner_bot = sponsor(2, "bot", "فروشگاه تایگو")
    service = SimpleNamespace(
        active_sponsors=AsyncMock(return_value=[channel, partner_bot]),
        set_sponsor_active=AsyncMock(),
    )
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=999)),
        get_chat_member=AsyncMock(
            side_effect=TelegramBadRequest(
                method=SimpleNamespace(), message="member list is inaccessible"
            )
        ),
    )

    deactivated = await deactivate_invalid_sponsors(service, bot)

    assert deactivated == [channel]
    service.set_sponsor_active.assert_awaited_once_with(channel.id, False)
