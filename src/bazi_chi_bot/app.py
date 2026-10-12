"""Application bootstrap for long polling."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat, User
from aiohttp_socks import (
    ProxyConnectionError as AiohttpSocksProxyConnectionError,
)
from aiohttp_socks import (
    ProxyError as AiohttpSocksProxyError,
)
from aiohttp_socks import (
    ProxyTimeoutError as AiohttpSocksProxyTimeoutError,
)

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
from .telegram.daily_worker import DailyChallengeWorker
from .telegram.groups import build_group_router
from .telegram.keyboards import configure_platform_links
from .telegram.platform import PlatformBot, PlatformName, to_domain_id
from .telegram.sponsors import deactivate_invalid_sponsors

logger = logging.getLogger(__name__)

RECONNECT_INTERVAL_SECONDS = 10 * 60

# aiohttp-socks exposes timeout and connection failures as separate exception
# classes which are not subclasses of TelegramNetworkError (and, in the
# current release, are not subclasses of one another either).  Treat all of
# them as a failed candidate during startup so the next proxy can be tried.
PROXY_CONNECTION_ERRORS = (
    AiohttpSocksProxyConnectionError,
    AiohttpSocksProxyError,
    AiohttpSocksProxyTimeoutError,
)


@dataclass(slots=True)
class TelegramConnection:
    bot: PlatformBot
    user: User
    proxy_number: int | None
    proxy_count: int
    platform: PlatformName = "telegram"


@dataclass(slots=True)
class StartupState:
    countdowns_queued: bool = False


def _build_bot(
    settings: Settings,
    proxy_url: str | None,
    *,
    platform: PlatformName = "telegram",
    token: str | None = None,
    api_base_url: str | None = None,
) -> PlatformBot:
    api = TelegramAPIServer.from_base(api_base_url) if api_base_url else None
    session = (
        AiohttpSession(proxy=proxy_url, **({"api": api} if api is not None else {}))
        if proxy_url is not None or api is not None
        else None
    )
    return PlatformBot(
        token=token or settings.bot_token,
        platform=platform,
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
        except (TelegramNetworkError, *PROXY_CONNECTION_ERRORS, TimeoutError, OSError) as error:
            if proxy_url is None:
                logger.warning("Direct Telegram connection is unavailable: %s", error)
            else:
                logger.warning(
                    "Telegram proxy %s/%s is unavailable: %s",
                    index,
                    len(candidates),
                    error,
                )
            await bot.session.close()
            continue

        return TelegramConnection(
            bot=bot,
            user=user,
            proxy_number=index if proxy_url is not None else None,
            proxy_count=len(proxy_urls),
            platform="telegram",
        )

    return None


async def _connect_to_bale(settings: Settings) -> TelegramConnection | None:
    """Return the first usable Bale connection, if a Bale token is configured."""
    if not settings.bale_bot_token:
        return None
    logger.info("Testing direct Bale connection")
    try:
        bot = _build_bot(
            settings,
            None,
            platform="bale",
            token=settings.bale_bot_token,
            api_base_url=settings.bale_api_base_url,
        )
        user = await bot.get_me()
    except ValueError:
        logger.warning("Bale API configuration is invalid")
        return None
    except (TelegramNetworkError, *PROXY_CONNECTION_ERRORS, TimeoutError, OSError) as error:
        logger.warning("Bale direct connection is unavailable: %s", error)
        await bot.session.close()
        return None
    return TelegramConnection(
        bot=bot,
        user=user,
        proxy_number=None,
        proxy_count=0,
        platform="bale",
    )


async def _register_bot_commands(bot: Bot, admin_ids: frozenset[int]) -> None:
    # Bale's documented Bot API does not expose Telegram's command-menu
    # methods. Commands still work when typed, so skip this optional API call.
    if getattr(bot, "platform", "telegram") == "bale":
        logger.info("Skipping Telegram command-menu registration for Bale")
        return
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
    await bot.set_my_commands(
        [BotCommand(command="play", description="ساخت مسابقهٔ چندنفرهٔ گروهی")],
        scope=BotCommandScopeAllGroupChats(),
    )
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
    """Verify that the selected messenger API is reachable every interval."""
    while True:
        await asyncio.sleep(interval)
        await bot.get_me()
        logger.debug(
            "%s connection health check succeeded",
            getattr(bot, "platform", "messenger").capitalize(),
        )


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
    external_admin_ids: frozenset[int],
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
    daily_worker = DailyChallengeWorker(service.daily, bot)
    dispatcher.include_router(
        build_router(
            service,
            countdown_service=countdown_service,
            countdown_scheduler=countdown_scheduler,
            countdown_admin_ids=admin_ids,
            payment_reviewer_ids=payment_reviewer_ids,
            countdown_timezone=countdown_timezone,
            platform=connection.platform,
        )
    )
    dispatcher.include_router(
        build_group_router(
            service,
            admin_ids=admin_ids | payment_reviewer_ids,
            platform=connection.platform,
        )
    )
    scheduler_task: asyncio.Task[None] | None = None
    daily_task: asyncio.Task[None] | None = None

    try:
        logger.info(
            "Connected to %s as @%s (%s)",
            connection.platform.capitalize(),
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
        await _register_bot_commands(bot, external_admin_ids)
        logger.info("Bot commands registered; starting long polling")
        if not startup_state.countdowns_queued:
            startup_countdowns = await countdown_service.make_all_active_due()
            startup_state.countdowns_queued = True
            logger.info("Queued %s active countdown(s) for startup delivery", startup_countdowns)
        scheduler_task = asyncio.create_task(
            countdown_scheduler.run(),
            name="countdown-scheduler",
        )
        daily_task = asyncio.create_task(daily_worker.run(), name="daily-challenge-worker")
        await _poll_with_connection_monitor(dispatcher, bot)
    finally:
        if daily_task is not None:
            await stop_scheduler(daily_task)
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
    telegram_countdown_admin_ids = parse_admin_ids(settings.admin_telegram_ids)
    telegram_payment_reviewer_ids = parse_admin_ids(settings.payment_reviewer_telegram_ids)
    bale_countdown_admin_ids = frozenset(
        to_domain_id("bale", value) for value in parse_admin_ids(settings.bale_admin_ids)
    )
    bale_payment_reviewer_ids = frozenset(
        to_domain_id("bale", value) for value in parse_admin_ids(settings.bale_payment_reviewer_ids)
    )
    all_admin_ids = (
        telegram_countdown_admin_ids
        | telegram_payment_reviewer_ids
        | bale_countdown_admin_ids
        | bale_payment_reviewer_ids
    )
    service = GameService(
        database,
        activation_exempt_ids=all_admin_ids,
        bale_payment_provider_token=settings.bale_payment_provider_token,
    )
    countdown_service = CountdownService(database, activation_exempt_ids=all_admin_ids)
    startup_state = StartupState()

    while True:
        telegram_connection, bale_connection = await asyncio.gather(
            _connect_to_telegram(settings), _connect_to_bale(settings)
        )
        connections = [
            connection
            for connection in (telegram_connection, bale_connection)
            if connection is not None
        ]
        if not connections:
            logger.error(
                "No messenger connection is available; testing all connections again in %s seconds",
                RECONNECT_INTERVAL_SECONDS,
            )
            await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)
            continue

        telegram_bot = telegram_connection.bot if telegram_connection else None
        bale_bot = bale_connection.bot if bale_connection else None
        if telegram_bot is not None:
            telegram_bot.peer = bale_bot
            telegram_bot.identity_resolver = service.platform_external_id
        if bale_bot is not None:
            bale_bot.peer = telegram_bot
            bale_bot.identity_resolver = service.platform_external_id
        configure_platform_links(
            telegram_username=telegram_connection.user.username if telegram_connection else None,
            bale_username=bale_connection.user.username if bale_connection else None,
        )

        try:
            await asyncio.gather(
                *(
                    _serve_connection(
                        connection,
                        service=service,
                        countdown_service=countdown_service,
                        admin_ids=(
                            telegram_countdown_admin_ids
                            if connection.platform == "telegram"
                            else bale_countdown_admin_ids
                        ),
                        payment_reviewer_ids=(
                            telegram_payment_reviewer_ids
                            if connection.platform == "telegram"
                            else bale_payment_reviewer_ids
                        ),
                        external_admin_ids=(
                            parse_admin_ids(settings.admin_telegram_ids)
                            | parse_admin_ids(settings.payment_reviewer_telegram_ids)
                            if connection.platform == "telegram"
                            else parse_admin_ids(settings.bale_admin_ids)
                            | parse_admin_ids(settings.bale_payment_reviewer_ids)
                        ),
                        countdown_timezone=settings.countdown_timezone,
                        startup_state=startup_state,
                    )
                    for connection in connections
                )
            )
        except (TelegramNetworkError, *PROXY_CONNECTION_ERRORS, TimeoutError, OSError):
            logger.warning("Messenger connection was lost; testing all configured connections again")
        else:
            return
        finally:
            for connection in connections:
                logger.info("Closing %s session", connection.platform.capitalize())
                await connection.bot.session.close()
