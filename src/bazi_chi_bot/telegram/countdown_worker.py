"""Telegram delivery adapter for persistent countdowns."""

import asyncio
import logging
import math
import time
from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from ..models import Countdown
from ..scheduling import FINAL_COUNTDOWN_TEXT, countdown_message
from ..services.countdowns import CountdownService

logger = logging.getLogger(__name__)


class CountdownScheduler:
    """Deliver active countdowns and wake immediately when an admin creates one."""

    def __init__(
        self,
        service: CountdownService,
        bot: Bot,
        timezone_name: str = "Asia/Tehran",
    ) -> None:
        self.service = service
        self.bot = bot
        self.timezone_name = timezone_name
        self._wake_event = asyncio.Event()

    def wake(self) -> None:
        self._wake_event.set()

    async def _wait(self, delay: float | None) -> None:
        if delay is None:
            await self._wake_event.wait()
            return
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=max(0, delay))
        except TimeoutError:
            pass

    async def run(self) -> None:
        logger.info("Countdown scheduler started")
        while True:
            # Clearing before the database read avoids losing a wake-up that
            # arrives between reading the next job and beginning the wait.
            self._wake_event.clear()
            countdown = await self.service.next_due()
            if countdown is None:
                await self._wait(None)
                continue

            now = int(time.time())
            if countdown.next_run_at > now:
                await self._wait(countdown.next_run_at - now)
                continue

            await self._deliver(countdown, now)

    async def _deliver(self, countdown: Countdown, now: int) -> None:
        daily_reminder = countdown.kind == "daily_reminder"
        if daily_reminder:
            user = await self.service.resolve_user(str(countdown.target_user_id))
            if not self.service.can_receive_daily_reminder(user):
                await self.service.cancel_daily_reminder(countdown.target_user_id)
                return
        if daily_reminder and now >= countdown.target_at + 5 * 60:
            # Never send yesterday's reminder after a restart or long outage.
            await self.service.record_daily_reminder_sent(countdown.id, now)
            return
        finished = not daily_reminder and now >= countdown.target_at
        reminder_minutes = str(
            max(1, math.ceil((countdown.target_at + 300 - now) / 60))
        ).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
        text = (
            f"⏰ <b>{reminder_minutes} "
            "دقیقه تا چالش روزانه!</b>\nآماده باش؛ بازی ساعت ۱۲ شروع می‌شود. 🎯"
            if daily_reminder
            else FINAL_COUNTDOWN_TEXT
            if finished
            else countdown_message(
                countdown.target_at - now,
                target_at=countdown.target_at,
                timezone_name=self.timezone_name,
            )
        )
        try:
            await self.bot.send_message(countdown.target_user_id, text)
        except TelegramForbiddenError:
            logger.warning(
                "Countdown recipient %s blocked or stopped the bot", countdown.target_user_id
            )
            if daily_reminder:
                await self.service.cancel_daily_reminder(countdown.target_user_id)
            else:
                await self.service.cancel_for_target(countdown.target_user_id)
        except TelegramBadRequest as error:
            logger.warning("Invalid countdown recipient %s: %s", countdown.target_user_id, error)
            if daily_reminder:
                await self.service.cancel_daily_reminder(countdown.target_user_id)
            else:
                await self.service.cancel_for_target(countdown.target_user_id)
        except TelegramRetryAfter as error:
            retry_after = max(1, math.ceil(error.retry_after))
            logger.warning("Countdown rate limited; retrying in %s seconds", retry_after)
            await self.service.retry_at(countdown.id, now + retry_after)
        except TelegramAPIError as error:
            logger.warning("Could not deliver countdown %s: %s", countdown.id, error)
            retry_at = now + 30 if finished else min(countdown.target_at, now + 30)
            await self.service.retry_at(countdown.id, retry_at)
        else:
            if daily_reminder:
                await self.service.record_daily_reminder_sent(countdown.id, now)
            elif finished:
                await self.service.complete(countdown.id, now)
            else:
                await self.service.record_sent(countdown.id, now, countdown.target_at)


async def stop_scheduler(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
