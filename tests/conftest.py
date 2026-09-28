from collections.abc import AsyncIterator

import pytest_asyncio

from bazi_chi_bot.db import Database
from bazi_chi_bot.game import GameService
from bazi_chi_bot.models import User


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "test.sqlite3")
    await database.initialize()
    yield database


@pytest_asyncio.fixture
async def service(database: Database) -> GameService:
    return GameService(database, choose_first_hider=lambda players: players[0])


@pytest_asyncio.fixture
async def players(service: GameService) -> tuple[User, User]:
    first = User(101, "sadegh", "صادق", None, "صادق")
    second = User(202, "friend", "دوست", "خوب", "دوست خوب")
    await service.save_user(first)
    await service.save_user(second)
    await service.add_question("truth", "راستش را بگو، چرا مشت ۳ را انتخاب کردی؟")
    await service.add_question("dare", "یک جرأت کوتاه انجام بده.")
    persisted_first = await service.get_user(first.telegram_id)
    persisted_second = await service.get_user(second.telegram_id)
    assert persisted_first is not None and persisted_second is not None
    return persisted_first, persisted_second
