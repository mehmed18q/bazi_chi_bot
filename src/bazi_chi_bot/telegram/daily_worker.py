"""Persistent start/end announcements for the Tehran daily challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..daily_schedule import challenge_window, local_date
from ..services.daily_challenges import DailyChallengeService

logger = logging.getLogger(__name__)


class DailyChallengeWorker:
    def __init__(self, service: DailyChallengeService, bot: Bot) -> None:
        self.service = service
        self.bot = bot

    async def run(self) -> None:
        while True:
            now = int(time.time())
            today = local_date(now)
            start, end = challenge_window(today)
            if start <= now < end:
                challenge = await self.service.ensure_today(now=now)
                await self.service.queue_notifications(challenge, "start")
            # Reconcile today's and yesterday's endings after restarts.
            for day in (today - timedelta(days=1), today):
                _, day_end = challenge_window(day)
                if now >= day_end:
                    challenge = await self.service.get(day)
                    if challenge is not None:
                        await self.service.close_day(day, now=now)
                        await self.service.queue_notifications(challenge, "end")
            await self._deliver_pending()
            next_boundary = min(
                (boundary for boundary in (start, end) if boundary > now), default=None
            )
            delay = min(20, max(0.1, next_boundary - time.time())) if next_boundary else 20
            await asyncio.sleep(delay)

    async def _deliver_pending(self) -> None:
        # Keep one pass bounded so a large broadcast does not postpone the
        # next schedule reconciliation indefinitely.
        deferred: set[tuple[str, int, str]] = set()
        for _ in range(100):
            item = await self.service.next_notification(deferred)
            if item is None:
                return
            day, user_id, event = item
            _, end = challenge_window(date.fromisoformat(day))
            if event == "start" and int(time.time()) >= end:
                await self.service.record_notification(day, user_id, event)
                continue
            text = await self.service.notification_text(day, user_id, event)
            try:
                await self.bot.send_message(
                    user_id,
                    text,
                    reply_markup=(
                        InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="🎮 ورود به چالش", callback_data="menu:daily_challenge"
                                    )
                                ]
                            ]
                        )
                        if event == "start"
                        else None
                    ),
                )
            except TelegramForbiddenError:
                await self.service.record_notification(day, user_id, event)
            except TelegramBadRequest as error:
                logger.warning("Daily challenge recipient %s is invalid: %s", user_id, error)
                await self.service.record_notification(day, user_id, event)
            except TelegramRetryAfter as error:
                await asyncio.sleep(max(1, error.retry_after))
                return
            except TelegramAPIError as error:
                logger.warning("Daily challenge notification for %s failed: %s", user_id, error)
                deferred.add(item)
                continue
            else:
                await self.service.record_notification(day, user_id, event)
