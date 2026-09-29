import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp_socks import ProxyTimeoutError
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe

from bazi_chi_bot import app
from bazi_chi_bot.config import Settings


TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"


def make_settings(**values: object) -> Settings:
    return Settings(_env_file=None, bot_token=TOKEN, **values)


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(GetMe(), "connection unavailable")


def fake_bot(*, user: object | None = None, error: Exception | None = None) -> object:
    get_me = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=user)
    return SimpleNamespace(
        get_me=get_me,
        session=SimpleNamespace(close=AsyncMock()),
    )


def test_proxy_urls_are_loaded_as_an_ordered_list(monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_PROXY_URLS",
        '["socks5://proxy-one:1080", " http://proxy-two:7890 ", "socks5://proxy-one:1080", ""]',
    )

    settings = Settings(_env_file=None, bot_token=TOKEN)

    assert settings.telegram_proxy_urls == [
        "socks5://proxy-one:1080",
        "http://proxy-two:7890",
    ]


@pytest.mark.asyncio
async def test_connection_tries_proxies_in_order_until_one_works(monkeypatch):
    unavailable = fake_bot(error=network_error())
    user = SimpleNamespace(id=42, username="test_bot")
    available = fake_bot(user=user)
    build_bot = Mock(side_effect=[unavailable, available])

    def build(settings: Settings, proxy_url: str | None) -> object:
        del settings
        return build_bot(proxy_url)

    monkeypatch.setattr(app, "_build_bot", build)
    settings = make_settings(telegram_proxy_urls=["socks5://first:1080", "http://second:7890"])

    connection = await app._connect_to_telegram(settings)

    assert connection is not None
    assert connection.bot is available
    assert connection.user is user
    assert connection.proxy_number == 2
    assert connection.proxy_count == 2
    assert [call.args[0] for call in build_bot.call_args_list] == [
        "socks5://first:1080",
        "http://second:7890",
    ]
    unavailable.session.close.assert_awaited_once()
    available.session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_timeout_fails_over_to_the_next_proxy(monkeypatch):
    timed_out = fake_bot(error=ProxyTimeoutError("proxy timed out"))
    user = SimpleNamespace(id=42, username="test_bot")
    available = fake_bot(user=user)
    build_bot = Mock(side_effect=[timed_out, available])

    def build(settings: Settings, proxy_url: str | None) -> object:
        del settings
        return build_bot(proxy_url)

    monkeypatch.setattr(app, "_build_bot", build)
    settings = make_settings(telegram_proxy_urls=["http://first:10808", "http://second:10808"])

    connection = await app._connect_to_telegram(settings)

    assert connection is not None
    assert connection.bot is available
    assert connection.proxy_number == 2
    timed_out.session.close.assert_awaited_once()
    available.session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_tests_every_proxy_before_reporting_failure(monkeypatch):
    bots = [fake_bot(error=network_error()) for _ in range(3)]
    build_bot = Mock(side_effect=bots)

    def build(settings: Settings, proxy_url: str | None) -> object:
        del settings
        return build_bot(proxy_url)

    monkeypatch.setattr(app, "_build_bot", build)
    settings = make_settings(telegram_proxy_urls=["proxy-1", "proxy-2", "proxy-3"])

    connection = await app._connect_to_telegram(settings)

    assert connection is None
    assert [call.args[0] for call in build_bot.call_args_list] == [
        "proxy-1",
        "proxy-2",
        "proxy-3",
    ]
    for bot in bots:
        bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_proxy_list_uses_direct_connection(monkeypatch):
    user = SimpleNamespace(id=42, username="test_bot")
    bot = fake_bot(user=user)
    build_bot = Mock(return_value=bot)

    def build(settings: Settings, proxy_url: str | None) -> object:
        del settings
        return build_bot(proxy_url)

    monkeypatch.setattr(app, "_build_bot", build)

    connection = await app._connect_to_telegram(make_settings())

    assert connection is not None
    assert connection.proxy_number is None
    build_bot.assert_called_once_with(None)


@pytest.mark.asyncio
async def test_connection_monitor_rechecks_and_propagates_network_failure(monkeypatch):
    bot = fake_bot(error=network_error())
    sleep = AsyncMock()
    monkeypatch.setattr(app.asyncio, "sleep", sleep)

    with pytest.raises(TelegramNetworkError):
        await app._monitor_connection(bot, interval=600)

    sleep.assert_awaited_once_with(600)
    bot.get_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_bot_waits_ten_minutes_when_every_connection_fails(monkeypatch, tmp_path):
    initialize = AsyncMock()
    monkeypatch.setattr(
        app,
        "Database",
        lambda path: SimpleNamespace(initialize=initialize, path=path),
    )
    monkeypatch.setattr(app, "GameService", Mock())
    monkeypatch.setattr(app, "CountdownService", Mock())
    connect = AsyncMock(return_value=None)
    monkeypatch.setattr(app, "_connect_to_telegram", connect)
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(app.asyncio, "sleep", sleep)

    settings = make_settings(
        database_path=tmp_path / "bot.sqlite3",
        payment_reviewer_telegram_ids="",
    )
    with pytest.raises(asyncio.CancelledError):
        await app.run_bot(settings)

    initialize.assert_awaited_once()
    connect.assert_awaited_once_with(settings)
    sleep.assert_awaited_once_with(10 * 60)
