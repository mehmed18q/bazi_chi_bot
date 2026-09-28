"""Application bootstrap for long polling."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import BotCommand, BotCommandScopeChat, User

from .config import Settings
from .countdown import (
    CountdownScheduler,
    CountdownService,
    parse_admin_ids,
    stop_scheduler,
)
from .db import Database
from .game import GameService
from .handlers import build_router
from .telegram.sponsors import deactivate_invalid_sponsors

logger = logging.getLogger(__name__)

RECONNECT_INTERVAL_SECONDS = 10 * 60


@dataclass(slots=True)
class TelegramConnection:
    bot: Bot
    user: User
    proxy_number: int | None
    proxy_count: int


@dataclass(slots=True)
class StartupState:
    countdowns_queued: bool = False


def _build_bot(settings: Settings, proxy_url: str | None) -> Bot:
    session = AiohttpSession(proxy=proxy_url) if proxy_url is not None else None
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def _connect_to_telegram(settings: Settings) -> TelegramConnection | None:
    """Return the first usable Telegram connection, testing proxies in order."""
    proxy_urls = settings.telegram_proxy_urls
    candidates: list[str | None] = list(proxy_urls) if proxy_urls else [None]

    for index, proxy_url in enumerate(candidates, start=1):
        if proxy_url is None:
            logger.info("Testing direct Telegram connection")
        else:
            logger.info("Testing Telegram proxy %s/%s", index, len(candidates))

        try:
            bot = _build_bot(settings, proxy_url)
        except ValueError:
            logger.warning("Telegram proxy %s/%s has an invalid URL", index, len(candidates))
            continue
        try:
            user = await bot.get_me()
        except TelegramNetworkError:
            if proxy_url is None:
                logger.warning("Direct Telegram connection is unavailable")
            else:
                logger.warning("Telegram proxy %s/%s is unavailable", index, len(candidates))
            await bot.session.close()
            continue

        return TelegramConnection(
            bot=bot,
            user=user,
            proxy_number=index if proxy_url is not None else None,
            proxy_count=len(proxy_urls),
        )

    return None


async def _register_bot_commands(bot: Bot, admin_ids: frozenset[int]) -> None:
    logger.info("Registering bot commands")
    public_commands = [
        BotCommand(command="start", description="شروع و منوی اصلی"),
        BotCommand(command="games", description="ادامهٔ بازی‌های نیمه‌تمام"),
        BotCommand(command="lastgame", description="آخرین نتیجهٔ من"),
        BotCommand(command="stats", description="آمار من"),
        BotCommand(command="leaderboard", description="برترین بازیکن‌ها"),
        BotCommand(command="help", description="راهنمای بازی"),
    ]
    await bot.set_my_commands(public_commands)
    admin_commands = public_commands + [
        BotCommand(command="countdown", description="ساخت شمارش‌معکوس"),
        BotCommand(command="countdowns", description="شمارش‌معکوس‌های فعال"),
        BotCommand(command="cancelcountdown", description="لغو شمارش‌معکوس"),
        BotCommand(command="users", description="فهرست کاربران ربات"),
        BotCommand(command="sponsors", description="مدیریت اسپانسرها"),
    ]
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except TelegramNetworkError:
            raise
        except TelegramAPIError as error:
            logger.warning("Could not register countdown commands for %s: %s", admin_id, error)


async def _monitor_connection(
    bot: Bot,
    interval: float = RECONNECT_INTERVAL_SECONDS,
) -> None:
    """Verify that the selected proxy can still reach Telegram every interval."""
    while True:
        await asyncio.sleep(interval)
        await bot.get_me()
        logger.debug("Telegram connection health check succeeded")


async def _poll_with_connection_monitor(dispatcher: Dispatcher, bot: Bot) -> None:
    polling_task = asyncio.create_task(
        dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        ),
        name="telegram-polling",
    )
    monitor_task = asyncio.create_task(
        _monitor_connection(bot),
        name="telegram-connection-monitor",
    )
    try:
        done, _ = await asyncio.wait(
            {polling_task, monitor_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if polling_task in done:
            await polling_task
            return

        monitor_error = monitor_task.exception()
        if not polling_task.done():
            await dispatcher.stop_polling()
        await polling_task
        if monitor_error is not None:
            raise monitor_error
        raise RuntimeError("Telegram connection monitor stopped unexpectedly")
    finally:
        for task in (polling_task, monitor_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(polling_task, monitor_task, return_exceptions=True)


async def _serve_connection(
    connection: TelegramConnection,
    *,
    service: GameService,
    countdown_service: CountdownService,
    admin_ids: frozenset[int],
    payment_reviewer_ids: frozenset[int],
    countdown_timezone: str,
    startup_state: StartupState,
) -> None:
    bot = connection.bot
    dispatcher = Dispatcher()
    countdown_scheduler = CountdownScheduler(
        countdown_service,
        bot,
        timezone_name=countdown_timezone,
    )
    dispatcher.include_router(
        build_router(
            service,
            countdown_service=countdown_service,
            countdown_scheduler=countdown_scheduler,
            countdown_admin_ids=admin_ids,
            payment_reviewer_ids=payment_reviewer_ids,
            countdown_timezone=countdown_timezone,
        )
    )
    scheduler_task: asyncio.Task[None] | None = None

    try:
        logger.info(
            "Connected to Telegram as @%s (%s)",
            connection.user.username,
            connection.user.id,
        )
        deactivated_sponsors = await deactivate_invalid_sponsors(service, bot)
        for sponsor in deactivated_sponsors:
            logger.warning(
                "Deactivated sponsor %s (%s): bot is not a channel administrator",
                sponsor.title,
                sponsor.chat_id,
            )
        await _register_bot_commands(bot, admin_ids)
        logger.info("Bot commands registered; starting long polling")
        if not startup_state.countdowns_queued:
            startup_countdowns = await countdown_service.make_all_active_due()
            startup_state.countdowns_queued = True
            logger.info("Queued %s active countdown(s) for startup delivery", startup_countdowns)
        scheduler_task = asyncio.create_task(
            countdown_scheduler.run(),
            name="countdown-scheduler",
        )
        await _poll_with_connection_monitor(dispatcher, bot)
    finally:
        if scheduler_task is not None:
            await stop_scheduler(scheduler_task)


async def run_bot(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger.info("Starting Bazi Chi bot")

    database = Database(settings.database_path)
    await database.initialize()
    logger.info("Database initialized at %s", settings.database_path)
    service = GameService(database)
    countdown_service = CountdownService(database)
    countdown_admin_ids = parse_admin_ids(settings.admin_telegram_ids)
    payment_reviewer_ids = parse_admin_ids(settings.payment_reviewer_telegram_ids)
    admin_ids = countdown_admin_ids | payment_reviewer_ids
    startup_state = StartupState()

    while True:
        connection = await _connect_to_telegram(settings)
        if connection is None:
            logger.error(
                "No Telegram connection is available; testing all connections again in %s seconds",
                RECONNECT_INTERVAL_SECONDS,
            )
            await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)
            continue

        if connection.proxy_number is None:
            logger.info("Using direct Telegram connection")
        else:
            logger.info(
                "Using Telegram proxy %s/%s",
                connection.proxy_number,
                connection.proxy_count,
            )

        try:
            await _serve_connection(
                connection,
                service=service,
                countdown_service=countdown_service,
                admin_ids=admin_ids,
                payment_reviewer_ids=payment_reviewer_ids,
                countdown_timezone=settings.countdown_timezone,
                startup_state=startup_state,
            )
        except TelegramNetworkError:
            logger.warning("Telegram connection was lost; testing all configured connections again")
        else:
            return
        finally:
            logger.info("Closing Telegram session")
            await connection.bot.session.close()
