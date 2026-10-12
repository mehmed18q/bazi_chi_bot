"""Small platform adapter shared by the Telegram and Bale bots.

Bale's Bot API follows Telegram's update and method shapes, so the existing
aiogram handlers can be reused.  The adapter only has to provide two things:
an API endpoint and a stable namespace for Bale user IDs.  Bale IDs are
stored as negative domain IDs (with ``-1`` reserved for the built-in bot
player) and are converted back immediately before an API request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import User

PlatformName = Literal["telegram", "bale"]


def to_domain_id(platform: PlatformName, external_id: int) -> int:
    """Return the ID used by the shared game/database layer."""
    return external_id if platform == "telegram" else -(external_id + 2)


def to_external_id(platform: PlatformName, domain_id: int) -> int:
    """Return a platform's native user/chat ID for an API request."""
    return domain_id if platform == "telegram" else -domain_id - 2


def is_domain_id_for_platform(platform: PlatformName, value: int) -> bool:
    if platform == "telegram":
        return value >= 0
    return value <= -2


def _integer_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


class PlatformIdentityMiddleware(BaseMiddleware):
    """Namespace ``from_user.id`` for updates received from Bale.

    aiogram's Telegram models are immutable.  A copied update gives handlers
    the canonical account ID in both ``from_user`` and private ``chat``
    fields.  Group chat IDs stay native because they are not user accounts;
    the receiving bot records those IDs so the adapter can send back through
    the same platform.
    """

    def __init__(self, platform: PlatformName, service=None) -> None:
        self.platform = platform
        self.service = service

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not isinstance(user, User):
            return await handler(event, data)

        domain_id = (
            await self.service.resolve_platform_user(self.platform, user.id)
            if self.service is not None
            else to_domain_id(self.platform, user.id)
        )
        event = event.model_copy(update={"from_user": user.model_copy(update={"id": domain_id})})

        # ``Message.answer`` and callback edits use the chat embedded in the
        # update.  For private chats it must use the same canonical account ID
        # as the handlers; otherwise a Bale external ID can be mistaken for a
        # Telegram ID by the outgoing adapter.
        event = self._canonicalize_chat(event, domain_id, data.get("bot"))
        return await handler(event, data)

    @staticmethod
    def _canonicalize_chat(event, domain_id: int, bot):
        chat = getattr(event, "chat", None)
        if chat is not None:
            if str(getattr(chat, "type", "")) == "private":
                return event.model_copy(update={"chat": chat.model_copy(update={"id": domain_id})})
            if bot is not None and hasattr(bot, "register_native_chat"):
                bot.register_native_chat(chat.id)
            return event

        message = getattr(event, "message", None)
        nested_chat = getattr(message, "chat", None)
        if nested_chat is None:
            return event
        if str(getattr(nested_chat, "type", "")) == "private":
            nested = message.model_copy(
                update={"chat": nested_chat.model_copy(update={"id": domain_id})}
            )
            return event.model_copy(update={"message": nested})
        if bot is not None and hasattr(bot, "register_native_chat"):
            bot.register_native_chat(nested_chat.id)
        return event


class PlatformBot(Bot):
    """aiogram ``Bot`` using either Telegram's or Bale's Bot API.

    ``peer`` is set once both bots are connected.  Outgoing private messages
    addressed to a player from the other platform are routed through that
    platform's bot, which lets one game include Telegram and Bale players.
    """

    platform: PlatformName
    peer: PlatformBot | None
    identity_resolver: Callable[[int, PlatformName], Awaitable[int | None]] | None

    def __init__(
        self,
        token: str,
        *,
        platform: PlatformName,
        session: BaseSession | None = None,
        default=None,
    ) -> None:
        super().__init__(token=token, session=session, default=default)
        self.platform = platform
        self.peer = None
        self.identity_resolver = None
        self._native_chat_ids: set[int] = set()

    def register_native_chat(self, chat_id: int) -> None:
        """Remember a group/channel ID that belongs to this bot's platform."""
        self._native_chat_ids.add(chat_id)

    def _is_native_chat(self, value: int) -> bool:
        # The registry handles all updates seen in this process.  The fallback
        # covers Telegram-style supergroup IDs after a restart.
        return value in self._native_chat_ids or value <= -10**11

    def accepts_domain_id(self, value: int) -> bool:
        return is_domain_id_for_platform(self.platform, value)

    def _external(self, value: object) -> object:
        number = _integer_id(value)
        if number is None or not is_domain_id_for_platform(self.platform, number):
            return value
        return to_external_id(self.platform, number)

    def _target_bot(self, value: object) -> tuple[PlatformBot, object]:
        number = _integer_id(value)
        if number is not None and not self.accepts_domain_id(number) and self.peer is not None:
            return self.peer, self.peer._external(number)
        return self, self._external(value)

    async def _external_for_current(self, value: object) -> object:
        number = _integer_id(value)
        if number is not None and self._is_native_chat(number):
            return number
        if number is not None and self.identity_resolver is not None:
            external = await self.identity_resolver(number, self.platform)
            if external is not None:
                return external
        return self._external(value)

    async def _target_bot_async(self, value: object) -> tuple[PlatformBot, object]:
        number = _integer_id(value)
        if number is not None:
            if self._is_native_chat(number):
                return self, number
            if self.peer is not None and self.peer._is_native_chat(number):
                return self.peer, number
        if number is not None and self.identity_resolver is not None:
            external = await self.identity_resolver(number, self.platform)
            if external is not None:
                return self, external
        if number is not None and self.peer is not None and self.peer.identity_resolver is not None:
            external = await self.peer.identity_resolver(number, self.peer.platform)
            if external is not None:
                return self.peer, external
        return self._target_bot(value)

    async def __call__(self, method, request_timeout=None):
        """Route aiogram method objects as well as direct bot helper calls.

        ``Message.answer`` and ``Message.edit_text`` invoke ``Bot.__call__``
        directly, so overriding only ``send_message`` would miss the most
        common handler path.  Rewrite IDs on a copied method object and
        execute it with the bot that owns the target platform.
        """
        target = self
        updates = {}
        chat_id = getattr(method, "chat_id", None)
        if chat_id is not None:
            target, external_chat_id = await self._target_bot_async(chat_id)
            updates["chat_id"] = external_chat_id
        elif hasattr(method, "user_id"):
            target, external_user_id = await self._target_bot_async(method.user_id)
            updates["user_id"] = external_user_id

        if hasattr(method, "from_chat_id"):
            updates["from_chat_id"] = await target._external_for_current(
                method.from_chat_id
            )
        if hasattr(method, "user_id") and "user_id" not in updates:
            updates["user_id"] = await target._external_for_current(method.user_id)
        if updates:
            method = method.model_copy(update=updates)
        return await Bot.__call__(target, method, request_timeout=request_timeout)

    async def send_message(self, chat_id, *args, **kwargs):
        return await Bot.send_message(self, chat_id, *args, **kwargs)

    async def send_photo(self, chat_id, *args, **kwargs):
        return await Bot.send_photo(self, chat_id, *args, **kwargs)

    async def send_document(self, chat_id, *args, **kwargs):
        return await Bot.send_document(self, chat_id, *args, **kwargs)

    async def send_audio(self, chat_id, *args, **kwargs):
        return await Bot.send_audio(self, chat_id, *args, **kwargs)

    async def send_video(self, chat_id, *args, **kwargs):
        return await Bot.send_video(self, chat_id, *args, **kwargs)

    async def forward_message(self, chat_id, from_chat_id, *args, **kwargs):
        return await Bot.forward_message(self, chat_id, from_chat_id, *args, **kwargs)

    async def copy_message(self, chat_id, from_chat_id, *args, **kwargs):
        return await Bot.copy_message(self, chat_id, from_chat_id, *args, **kwargs)

    async def edit_message_text(self, *args, **kwargs):
        return await Bot.edit_message_text(self, *args, **kwargs)

    async def edit_message_caption(self, *args, **kwargs):
        return await Bot.edit_message_caption(self, *args, **kwargs)

    async def delete_message(self, chat_id, *args, **kwargs):
        return await Bot.delete_message(self, chat_id, *args, **kwargs)

    async def get_chat_member(self, chat_id, user_id, *args, **kwargs):
        return await Bot.get_chat_member(self, chat_id, user_id, *args, **kwargs)

    async def get_user_profile_photos(self, user_id, *args, **kwargs):
        return await Bot.get_user_profile_photos(self, user_id, *args, **kwargs)

    async def get_chat(self, chat_id, *args, **kwargs):
        return await Bot.get_chat(self, chat_id, *args, **kwargs)

    async def leave_chat(self, chat_id, *args, **kwargs):
        return await Bot.leave_chat(self, chat_id, *args, **kwargs)
