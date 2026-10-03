"""Mandatory sponsor membership checks and Telegram middleware."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..game import GameService
from ..persistence.sponsors import Sponsor
from .access import is_free_event
from .keyboards import required_sponsors_keyboard
from .shared import ProfileSetup

logger = logging.getLogger(__name__)

_ACTIVE_MEMBER_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
}


@dataclass(frozen=True, slots=True)
class SponsorCheck:
    sponsors: tuple[Sponsor, ...]
    missing_channels: tuple[Sponsor, ...]
    unavailable_channels: tuple[Sponsor, ...]

    @property
    def allowed(self) -> bool:
        return not self.missing_channels and not self.unavailable_channels


def is_active_member(member: Any) -> bool:
    """Handle regular, admin, owner and restricted-but-still-member responses."""
    return member.status in _ACTIVE_MEMBER_STATUSES or (
        member.status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False))
    )


async def check_sponsors(
    bot: Bot,
    user_id: int,
    sponsors: Sequence[Sponsor],
) -> SponsorCheck:
    missing: list[Sponsor] = []
    unavailable: list[Sponsor] = []
    for sponsor in sponsors:
        # Telegram has no API for checking whether a user started another bot.
        if sponsor.kind == "bot":
            continue
        if not sponsor.chat_id:
            unavailable.append(sponsor)
            continue
        try:
            member = await bot.get_chat_member(sponsor.chat_id, user_id)
        except TelegramAPIError as error:
            logger.warning(
                "Could not check sponsor channel %s (%s): %s",
                sponsor.id,
                sponsor.chat_id,
                error,
            )
            unavailable.append(sponsor)
        else:
            if not is_active_member(member):
                missing.append(sponsor)
    return SponsorCheck(tuple(sponsors), tuple(missing), tuple(unavailable))


async def channel_admin_status(bot: Bot, sponsor: Sponsor) -> bool | None:
    """True/False means verified; None means a transient check failure."""
    if sponsor.kind != "channel" or not sponsor.chat_id:
        return False
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(sponsor.chat_id, me.id)
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        logger.warning("Sponsor channel %s is inaccessible: %s", sponsor.id, error)
        return False
    except TelegramAPIError as error:
        logger.warning("Sponsor channel %s could not be verified: %s", sponsor.id, error)
        return None
    return member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
    }


async def deactivate_invalid_sponsors(service: GameService, bot: Bot) -> list[Sponsor]:
    """Disable active channels that definitively lack bot-admin access."""
    deactivated: list[Sponsor] = []
    for sponsor in await service.active_sponsors():
        if sponsor.kind != "channel":
            continue
        if await channel_admin_status(bot, sponsor) is False:
            await service.set_sponsor_active(sponsor.id, False)
            deactivated.append(sponsor)
    return deactivated


def sponsor_gate_text(check: SponsorCheck) -> str:
    if check.unavailable_channels:
        names = "، ".join(escape(sponsor.title) for sponsor in check.unavailable_channels)
        return (
            "⚠️ <b>بررسی عضویت موقتاً ممکن نیست.</b>\n\n"
            f"ربات نتوانست عضویت در «{names}» را بررسی کند. کمی بعد دوباره تلاش کن؛ "
            "اگر مشکل ادامه داشت، مدیر کانال باید دسترسی بازی‌چی را بررسی کند."
        )
    return (
        "🔒 <b>برای استفاده از بازی‌چی، ابتدا عضو اسپانسرهای زیر شو.</b>\n\n"
        "بعد از عضویت روی «بررسی عضویت» بزن."
    )


async def show_sponsor_gate(
    event: Message | CallbackQuery,
    check: SponsorCheck,
    *,
    payload: str = "",
) -> None:
    keyboard = required_sponsors_keyboard(list(check.sponsors), payload)
    text = sponsor_gate_text(check)
    if isinstance(event, CallbackQuery):
        if event.message is not None:
            await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer("ابتدا عضویتت را تکمیل کن.", show_alert=True)
    else:
        await event.answer(text, reply_markup=keyboard)


class SponsorMembershipMiddleware(BaseMiddleware):
    """Prevent bypassing the sponsor gate through old buttons or direct commands."""

    def __init__(self, service: GameService, admin_ids: frozenset[int]) -> None:
        self.service = service
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)) or event.from_user is None:
            return await handler(event, data)
        if event.from_user.id in self.admin_ids:
            return await handler(event, data)
        if await is_free_event(self.service, event):
            return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("sponsors:check"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("profile:"):
            return await handler(event, data)

        state = data.get("state")
        if (
            isinstance(event, Message)
            and state is not None
            and await state.get_state() == ProfileSetup.waiting_for_name.state
        ):
            return await handler(event, data)

        user = await self.service.get_user(event.from_user.id)
        if user is None or not user.is_activated:
            # The payment middleware owns access control until activation.
            return await handler(event, data)

        sponsors = await self.service.active_sponsors()
        if not sponsors:
            return await handler(event, data)
        check = await check_sponsors(data["bot"], event.from_user.id, sponsors)
        if check.allowed:
            return await handler(event, data)
        await show_sponsor_gate(event, check)
        return None
