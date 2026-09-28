"""SQLite connection management and schema migrations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .persistence.migrations import MIGRATIONS as MIGRATIONS


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as connection:
            row = await (await connection.execute("PRAGMA user_version")).fetchone()
            current_version = int(row[0])
            if current_version > len(MIGRATIONS):
                raise RuntimeError(
                    f"Database schema version {current_version} is newer than supported "
                    f"version {len(MIGRATIONS)}"
                )
            for index, migration in enumerate(MIGRATIONS[current_version:], current_version + 1):
                # executescript otherwise commits before running. Wrapping the schema and
                # version bump in the script keeps each migration crash-safe and atomic.
                await connection.executescript(
                    f"BEGIN IMMEDIATE;\n{migration}\nPRAGMA user_version = {index};\nCOMMIT;"
                )
            journal_mode = await (await connection.execute("PRAGMA journal_mode")).fetchone()
            if str(journal_mode[0]).lower() != "wal":
                await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA synchronous = NORMAL")
            await connection.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path, timeout=10)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
